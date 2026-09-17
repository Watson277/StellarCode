"""Role-scoped Agent used by team mode with isolated history and skill context."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from stellarcode.agent import ChatClient
from stellarcode.cancellation import (
    TaskCancelledError,
    cancellable_call,
    raise_if_cancelled,
)
from stellarcode.image import ImageReferenceParser, image_tool_message, prune_historical_images
from stellarcode.llm.types import chat_with_optional_delta, llm_operation, normalize_chat_result
from stellarcode.memory import ConversationHistoryCompactor, MemoryManager
from stellarcode.multi_agent.message import AgentMessage, MessageType
from stellarcode.multi_agent.role import AgentRole
from stellarcode.prompt import (
    ContextKind,
    PromptAssembler,
    PromptContext,
    PromptLayer,
    PromptMode,
    PromptSnapshot,
    publish_prompt_snapshot,
    runtime_context,
    strip_internal_context_metadata,
    untrusted_context_message,
    without_context_messages,
)
from stellarcode.skill import (
    SkillContextBuffer,
    SkillRegistry,
    activate_skill_context,
    format_skill_index,
)
from stellarcode.tools import ToolExecutionResult, ToolInvocation, ToolRegistry


PLANNER_PROMPT = """## Team planner role

You are the planner in a multi-agent coding team.

Analyze the user's goal and return JSON only. Do not call tools and do not execute the task.
Use this exact shape:
{
  "summary": "short summary",
  "steps": [
    {
      "id": "step_1",
      "description": "one concrete executable step",
      "type": "FILE_READ | FILE_WRITE | COMMAND | ANALYSIS | VERIFICATION",
      "dependencies": []
    }
  ]
}

Keep simple work to 1-3 steps and complex work to 5-10 steps. Every step must be concrete,
bounded, and independently reviewable. Dependencies must refer to other step ids. Include
verification for code or file changes, and do not add work outside the user's requested
scope.
"""

WORKER_PROMPT = """## Team worker role

You are a worker in a multi-agent coding team.

Execute only the assigned step. Use the available tools when they help, and return a
clear result describing what changed, what evidence was observed, what verification ran,
and any blocker. Do not redesign the overall plan or perform adjacent tasks. Treat the
overall goal, dependency results, and reviewer feedback as bounded context for this step,
not permission to expand it.
"""

REVIEWER_PROMPT = """## Team reviewer role

You are the reviewer in a multi-agent coding team.

Check whether the execution result is correct, complete, and consistent with the task.
Do not call tools. Return JSON only:
{
  "approved": true,
  "summary": "review summary",
  "issues": [],
  "suggestions": []
}
Use approved=false when required evidence is missing, the result is incorrect, the task
scope was exceeded, or claimed verification is unsupported. Keep issues specific and
actionable; do not invent evidence.
"""


ROLE_PROMPTS = {
    AgentRole.PLANNER: PLANNER_PROMPT,
    AgentRole.WORKER: WORKER_PROMPT,
    AgentRole.REVIEWER: REVIEWER_PROMPT,
}

ROLE_PROMPT_MODES = {
    AgentRole.PLANNER: PromptMode.TEAM_PLANNER,
    AgentRole.WORKER: PromptMode.TEAM_WORKER,
    AgentRole.REVIEWER: PromptMode.TEAM_REVIEWER,
}


class SubAgent:
    """A role-scoped agent with isolated history and shared LLM/tools."""

    def __init__(
        self,
        name: str,
        role: AgentRole,
        llm_client: ChatClient,
        tool_registry: ToolRegistry,
        max_iterations: int = 6,
        memory_manager: MemoryManager | None = None,
        system_prompt: str | None = None,
        max_web_search_calls: int = 4,
        skill_registry: SkillRegistry | None = None,
        skill_context_buffer: SkillContextBuffer | None = None,
        workspace: str | Path | None = None,
        context_window: int = 200_000,
        rag_auto_retrieval: bool | None = True,
        event_callback: Callable[[str, dict[str, Any]], None] | None = None,
        prompt_assembler: PromptAssembler | None = None,
    ) -> None:
        if max_web_search_calls < 1:
            raise ValueError("max_web_search_calls must be at least 1.")
        self.name = name
        self.role = role
        self.llm_client = llm_client
        self.tool_registry = tool_registry
        self.max_iterations = max_iterations
        self.memory_manager = memory_manager
        self.base_system_prompt = system_prompt or ROLE_PROMPTS[role]
        self.prompt_mode = ROLE_PROMPT_MODES[role]
        self.prompt_assembler = prompt_assembler or PromptAssembler()
        self._current_memory_context = ""
        self._last_prompt_snapshot: PromptSnapshot | None = None
        self.max_web_search_calls = max_web_search_calls
        self._web_search_calls = 0
        self.skill_registry = skill_registry
        self.skill_context_buffer = skill_context_buffer
        self.workspace = Path(workspace or ".").resolve()
        self.image_parser = ImageReferenceParser(self.workspace)
        self._current_query = ""
        self.context_window = context_window
        self.rag_auto_retrieval = rag_auto_retrieval
        self.history_compactor = ConversationHistoryCompactor(context_window=context_window)
        self.event_callback = event_callback
        self._team_task_id: str | None = None
        self.messages: list[dict[str, Any]] = [
            {"role": "system", "content": self.base_system_prompt}
        ]

    @property
    def should_use_tools(self) -> bool:
        return self.role == AgentRole.WORKER

    def clear_history(self) -> None:
        self.messages = [{"role": "system", "content": self.base_system_prompt}]
        self._current_memory_context = ""
        self._last_prompt_snapshot = None
        self.history_compactor.reset()
        if self.skill_context_buffer:
            self.skill_context_buffer.clear()

    def execute(
        self,
        task: AgentMessage,
        cancellation_event: threading.Event | None = None,
        *,
        team_task_id: str | None = None,
    ) -> AgentMessage:
        previous_task_id = self._team_task_id
        self._team_task_id = team_task_id
        try:
            return self._execute_task(task, cancellation_event)
        finally:
            self._team_task_id = previous_task_id

    def _execute_task(
        self,
        task: AgentMessage,
        cancellation_event: threading.Event | None = None,
    ) -> AgentMessage:
        raise_if_cancelled(cancellation_event)
        if task.type != MessageType.TASK:
            return AgentMessage.error(
                self.name,
                self.role,
                f"Expected TASK message, received {task.type.value}.",
            )

        self._web_search_calls = 0
        self._current_query = task.content
        prune_historical_images(self.messages)
        self._refresh_system_prompt(task.content, include_memory_context=True)
        self.messages.append(
            self.image_parser.user_message(self._prepend_skill_bodies(task.content))
        )

        for _ in range(self.max_iterations):
            raise_if_cancelled(cancellation_event)
            try:
                response = self._chat(cancellation_event)
            except TaskCancelledError:
                raise
            except Exception as exc:
                return AgentMessage.error(self.name, self.role, f"LLM call failed: {exc}")

            self.messages.append(response)
            tool_calls = response.get("tool_calls") or []
            if tool_calls:
                if not self.should_use_tools:
                    return AgentMessage.error(
                        self.name,
                        self.role,
                        f"{self.role.value} is not allowed to call tools.",
                    )
                self.messages.extend(self._execute_tool_calls(tool_calls, cancellation_event))
                self._append_loaded_skill_context()
                raise_if_cancelled(cancellation_event)
                continue

            return AgentMessage.result(
                self.name,
                self.role,
                str(response.get("content") or ""),
            )

        return AgentMessage.error(
            self.name,
            self.role,
            f"Stopped after {self.max_iterations} iterations without a final result.",
        )

    def execute_with_context(
        self,
        task: AgentMessage,
        context: str,
        cancellation_event: threading.Event | None = None,
        *,
        team_task_id: str | None = None,
    ) -> AgentMessage:
        content = task.content
        if context.strip():
            content = f"{context.strip()}\n\nCurrent task:\n{task.content}"
        return self.execute(
            AgentMessage.task(task.from_agent, content),
            cancellation_event,
            team_task_id=team_task_id,
        )

    def review(
        self,
        original_task: str,
        execution_result: str,
        cancellation_event: threading.Event | None = None,
    ) -> AgentMessage:
        review_input = f"Original task:\n{original_task}\n\nExecution result:\n{execution_result}"
        return self.execute(
            AgentMessage.task("orchestrator", review_input),
            cancellation_event,
        )

    def _execute_tool_calls(
        self,
        tool_calls: list[dict[str, Any]],
        cancellation_event: threading.Event | None = None,
    ) -> list[dict[str, Any]]:
        invocations = []
        for tool_call in tool_calls:
            function = tool_call.get("function") or {}
            invocations.append(
                ToolInvocation(
                    id=str(tool_call.get("id") or "unknown_tool_call"),
                    name=str(function.get("name") or "unknown_tool"),
                    arguments=function.get("arguments"),
                )
            )
        for invocation in invocations:
            self._emit_team_event(
                "team.agent.tool.started",
                {
                    "agent_name": self.name,
                    "agent_role": self.role.value.lower(),
                    "team_task_id": self._team_task_id or "",
                    "tool_call_id": invocation.id,
                    "name": invocation.name,
                    "arguments": self.tool_registry.event_arguments(
                        invocation.name,
                        invocation.arguments,
                    ),
                },
            )
        runnable: list[ToolInvocation] = []
        runnable_indexes: list[int] = []
        results_by_index: list[ToolExecutionResult | None] = [None] * len(invocations)
        for index, invocation in enumerate(invocations):
            if invocation.name == "web_search":
                if self._web_search_calls >= self.max_web_search_calls:
                    results_by_index[index] = ToolExecutionResult(
                        id=invocation.id,
                        name=invocation.name,
                        arguments=invocation.arguments,
                        result=(
                            "[WEB_POLICY] Search call limit reached "
                            f"({self.max_web_search_calls} per task). Use earlier URLs "
                            "with web_fetch or finish from existing evidence."
                        ),
                        elapsed_ms=0,
                        success=False,
                    )
                    continue
                self._web_search_calls += 1
            runnable.append(invocation)
            runnable_indexes.append(index)
        with activate_skill_context(self.skill_context_buffer):
            executed = self.tool_registry.execute_tools(
                runnable,
                cancellation_event=cancellation_event,
            )
        for index, result in zip(runnable_indexes, executed):
            results_by_index[index] = result
        results = [result for result in results_by_index if result is not None]
        for result in results:
            self._emit_team_event(
                "team.agent.tool.completed",
                {
                    "agent_name": self.name,
                    "agent_role": self.role.value.lower(),
                    "team_task_id": self._team_task_id or "",
                    "tool_call_id": result.id,
                    "name": result.name,
                    "result_preview": _team_event_text(result.result, limit=1_600),
                    "elapsed_ms": result.elapsed_ms,
                    "success": result.success,
                    "timed_out": result.timed_out,
                },
            )
        messages = [
            {
                "role": "tool",
                "tool_call_id": result.id,
                "name": result.name,
                "content": result.result,
            }
            for result in results
        ]
        for result in results:
            image_message = image_tool_message(result.name, result.image_parts)
            if image_message is not None:
                messages.append(image_message)
        return messages

    def _emit_team_event(self, event_type: str, data: dict[str, Any]) -> None:
        if not self.event_callback:
            return
        try:
            self.event_callback(event_type, data)
        except Exception:
            pass

    def _refresh_system_prompt(
        self,
        query: str,
        *,
        available_tools: frozenset[str] | None = None,
        include_memory_context: bool = False,
        publish_snapshot: bool = True,
    ) -> None:
        skill_index = ""
        if self.skill_registry:
            skill_index = format_skill_index(self.skill_registry.enabled_skills())
        if include_memory_context and self.memory_manager:
            self._current_memory_context = self.memory_manager.build_context_for_query(query)
        assembly = self.prompt_assembler.assemble(
            self.prompt_mode,
            PromptContext(
                base_prompt=self.base_system_prompt,
                runtime_context=runtime_context(),
                skill_index=skill_index,
                memory_context=self._current_memory_context,
                available_tools=(
                    available_tools
                    if available_tools is not None
                    else (
                        frozenset(tool.name for tool in self.tool_registry.list_tools())
                        if self.should_use_tools
                        else frozenset()
                    )
                ),
                rag_auto_retrieval=self.rag_auto_retrieval,
            ),
        )
        system_message = {"role": "system", "content": assembly.system_prompt}
        if self.messages and self.messages[0].get("role") == "system":
            self.messages[0] = system_message
        else:
            self.messages.insert(0, system_message)
        self.messages = self.history_compactor.sync_summary_context(self.messages)

        summary_layers: list[PromptLayer] = []
        summary_message = untrusted_context_message(
            ContextKind.CONVERSATION_SUMMARY,
            self.history_compactor.summary,
        )
        if summary_message is not None:
            summary_layers.append(
                PromptLayer(
                    "conversation_summary",
                    "user",
                    str(summary_message["content"]),
                    sensitive=True,
                )
            )
        self._last_prompt_snapshot = assembly.snapshot(
            self.prompt_mode,
            additional_layers=summary_layers,
        )
        if publish_snapshot:
            publish_prompt_snapshot(self.llm_client, self._last_prompt_snapshot)

        if include_memory_context:
            self.messages = without_context_messages(
                self.messages,
                [ContextKind.RETRIEVED_MEMORY],
            )
            self.messages.extend(assembly.context_messages)

    def prompt_snapshot(self, *, include_sensitive: bool = False) -> dict[str, Any]:
        """Return this role's current prompt with sensitive context redacted by default."""

        if self._last_prompt_snapshot is None:
            self._refresh_system_prompt("", publish_snapshot=False)
        assert self._last_prompt_snapshot is not None
        return self._last_prompt_snapshot.to_dict(include_sensitive=include_sensitive)

    def _prepend_skill_bodies(self, content: str) -> str:
        if not self.skill_context_buffer:
            return content
        loaded = self.skill_context_buffer.drain()
        return f"{loaded}\n{content}" if loaded else content

    def _append_loaded_skill_context(self) -> None:
        """Make a tool-loaded Skill available in the very next model round."""

        if not self.skill_context_buffer:
            return
        loaded = self.skill_context_buffer.drain()
        if not loaded:
            return
        self.messages.append(
            {
                "role": "user",
                "content": (
                    f"{loaded}\n"
                    "Use this guidance for the current assigned step and continue from the "
                    "tool results above."
                ),
            }
        )

    def _chat(
        self,
        cancellation_event: threading.Event | None,
    ) -> dict[str, Any]:
        tool_definitions = self.tool_registry.list_tools() if self.should_use_tools else []
        available_tools = frozenset(tool.name for tool in tool_definitions)
        tools = (
            [tool.to_openai_tool() for tool in tool_definitions] if self.should_use_tools else None
        )
        self._refresh_system_prompt(
            self._current_query,
            available_tools=available_tools,
        )
        compaction = self.history_compactor.maybe_compact(
            self.messages,
            tools,
            self.llm_client,
            cancellation_event,
        )
        if compaction is not None:
            self.messages = compaction.messages
            self._refresh_system_prompt(
                self._current_query,
                available_tools=available_tools,
            )
        pending_delta: list[str] = []
        pending_chars = 0
        last_flush = time.monotonic()
        emitted_delta = False

        def flush_delta() -> None:
            nonlocal emitted_delta, pending_chars, last_flush
            if cancellation_event is not None and cancellation_event.is_set():
                pending_delta.clear()
                pending_chars = 0
                return
            if not pending_delta:
                return
            text = "".join(pending_delta)
            pending_delta.clear()
            pending_chars = 0
            last_flush = time.monotonic()
            emitted_delta = True
            self._emit_team_event(
                "team.agent.delta",
                {
                    "agent_name": self.name,
                    "agent_role": self.role.value.lower(),
                    "team_task_id": self._team_task_id or "",
                    "text": text,
                },
            )

        def collect_delta(text: str) -> None:
            nonlocal pending_chars
            if not text or cancellation_event is not None and cancellation_event.is_set():
                return
            pending_delta.append(text)
            pending_chars += len(text)
            if pending_chars >= 48 or time.monotonic() - last_flush >= 0.05:
                flush_delta()

        with llm_operation(f"team-{self.role.value.lower()}"):
            try:
                provider_messages = strip_internal_context_metadata(self.messages)
                raw = cancellable_call(
                    lambda: chat_with_optional_delta(
                        self.llm_client,
                        provider_messages,
                        tools=tools,
                        temperature=0.2,
                        on_delta=collect_delta if self.event_callback is not None else None,
                    ),
                    cancellation_event,
                )
            finally:
                flush_delta()
        result = normalize_chat_result(
            raw,
            client=self.llm_client,
            messages=provider_messages,
            tools=tools,
        )
        if emitted_delta and result.message.get("tool_calls"):
            self._emit_team_event(
                "team.agent.delta",
                {
                    "agent_name": self.name,
                    "agent_role": self.role.value.lower(),
                    "team_task_id": self._team_task_id or "",
                    "text": "",
                    "reset": True,
                },
            )
        if self.memory_manager:
            self.memory_manager.token_budget.record_usage(
                result.usage.input_tokens,
                result.usage.output_tokens,
            )
        return result.message


def _team_event_text(value: str, limit: int = 1_600) -> str:
    """Keep child-agent tool output useful without flooding event replay."""

    text = value.strip()
    return text if len(text) <= limit else f"{text[:limit]}\n… [truncated]"
