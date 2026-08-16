from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

from stellarcode.agent import ChatClient, runtime_context
from stellarcode.cancellation import (
    TaskCancelledError,
    cancellable_call,
    raise_if_cancelled,
)
from stellarcode.image import ImageReferenceParser, image_tool_message, prune_historical_images
from stellarcode.llm.types import llm_operation, normalize_chat_result
from stellarcode.memory import ConversationHistoryCompactor, MemoryManager
from stellarcode.multi_agent.message import AgentMessage, MessageType
from stellarcode.multi_agent.role import AgentRole
from stellarcode.skill import (
    SkillContextBuffer,
    SkillRegistry,
    activate_skill_context,
    format_skill_index,
)
from stellarcode.tools import ToolExecutionResult, ToolInvocation, ToolRegistry


PLANNER_PROMPT = """You are the planner in a multi-agent coding team.

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

Keep simple work to 1-3 steps and complex work to 5-10 steps. Dependencies must refer
to other step ids. Include verification for code or file changes.
"""

WORKER_PROMPT = """You are a worker in a multi-agent coding team.

Execute only the assigned step. Use the available tools when they help, and return a
clear result describing what was done and what evidence was observed. Do not redesign
the overall plan. Treat dependency context as read-only evidence from earlier steps.
Use list_dir to inspect directories and delete_file for file deletion. Never report a
filesystem change as successful until the tool result confirms it.
File tools accept absolute paths and may access locations outside the working directory.
Tools starting with mcp__ are dynamically provided by configured MCP servers. Use only
the MCP tools present in the supplied tool schemas.
Use web_search for current, recent, or uncertain public information. Use web_fetch for
a known HTTP or HTTPS URL, or to read a result page found by web_search.
Try web_fetch once for ordinary public pages. If it fails, returns empty or blocked
content, or the task requires JavaScript, interaction, console logs, or network
inspection, use the available chrome-devtools MCP tools. Known static-fetch-resistant
sites such as WeChat article pages may use the browser directly. After navigation and
any wait_for, prefer mcp__chrome-devtools__take_snapshot for readable DOM text; use
take_screenshot only for an explicitly requested image. Prefer fill_form for multiple
fields.
Chrome starts isolated. If the assigned task requires the user's existing login state,
call browser_status and browser_connect, then inspect browser_tabs. Disconnect after the
shared session is no longer needed. Never try to close a user-owned shared Chrome tab.
For private repositories or authenticated pages, stop and report the exact setup error
when browser_connect fails. Do not fall back to git, gh, web_fetch, or execute_command
unless the user explicitly requested separately configured command-line credentials.
Inspect Search quality and Fetch guidance. A low-quality search does not prove that a
fact is unavailable. Fetch at most three relevant pages, and do not keep inventing
slightly different searches after the available search budget is exhausted.
Return independent tool calls together in one response so they can run in parallel.
Keep dependent tool calls in separate rounds.
Avoid full-disk recursive scans; narrow exploration with list_dir and search_code.
For codebase-understanding tasks, follow the search_code tool's retrieval policy, then use read_file
when exact surrounding source is needed.
Inputs can contain @image:<path> or @clipboard attachments. Inspect attached image
content directly and do not infer it from a filename.
"""

REVIEWER_PROMPT = """You are the reviewer in a multi-agent coding team.

Check whether the execution result is correct, complete, and consistent with the task.
Do not call tools. Return JSON only:
{
  "approved": true,
  "summary": "review summary",
  "issues": [],
  "suggestions": []
}
Use approved=false when evidence is missing or the result is incorrect.
"""


ROLE_PROMPTS = {
    AgentRole.PLANNER: PLANNER_PROMPT,
    AgentRole.WORKER: WORKER_PROMPT,
    AgentRole.REVIEWER: REVIEWER_PROMPT,
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
        self.max_web_search_calls = max_web_search_calls
        self._web_search_calls = 0
        self.skill_registry = skill_registry
        self.skill_context_buffer = skill_context_buffer
        self.workspace = Path(workspace or ".").resolve()
        self.image_parser = ImageReferenceParser(self.workspace)
        self._current_query = ""
        self.context_window = context_window
        self.history_compactor = ConversationHistoryCompactor(context_window=context_window)
        self.messages: list[dict[str, Any]] = [
            {"role": "system", "content": self.base_system_prompt}
        ]

    @property
    def should_use_tools(self) -> bool:
        return self.role == AgentRole.WORKER

    def clear_history(self) -> None:
        self.messages = [{"role": "system", "content": self.base_system_prompt}]
        self.history_compactor.reset()
        if self.skill_context_buffer:
            self.skill_context_buffer.clear()

    def execute(
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
        self._refresh_system_prompt(task.content)
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
                self.messages.extend(
                    self._execute_tool_calls(tool_calls, cancellation_event)
                )
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
    ) -> AgentMessage:
        content = task.content
        if context.strip():
            content = f"{context.strip()}\n\nCurrent task:\n{task.content}"
        return self.execute(
            AgentMessage.task(task.from_agent, content),
            cancellation_event,
        )

    def review(
        self,
        original_task: str,
        execution_result: str,
        cancellation_event: threading.Event | None = None,
    ) -> AgentMessage:
        review_input = (
            f"Original task:\n{original_task}\n\n"
            f"Execution result:\n{execution_result}"
        )
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

    def _refresh_system_prompt(self, query: str) -> None:
        prompt = f"{self.base_system_prompt}\n\n{runtime_context()}"
        if self.skill_registry:
            skill_index = format_skill_index(self.skill_registry.enabled_skills())
            if skill_index:
                prompt = f"{prompt}\n\n{skill_index}"
        if self.memory_manager:
            memory_context = self.memory_manager.build_context_for_query(query)
            if memory_context:
                prompt = f"{prompt}\n\n{memory_context}"
        self.messages[0] = {
            "role": "system",
            "content": self.history_compactor.decorate_system_prompt(prompt),
        }

    def _prepend_skill_bodies(self, content: str) -> str:
        if not self.skill_context_buffer:
            return content
        loaded = self.skill_context_buffer.drain()
        return f"{loaded}\n{content}" if loaded else content

    def _chat(
        self,
        cancellation_event: threading.Event | None,
    ) -> dict[str, Any]:
        tools = self.tool_registry.schemas() if self.should_use_tools else None
        compaction = self.history_compactor.maybe_compact(
            self.messages,
            tools,
            self.llm_client,
            cancellation_event,
        )
        if compaction is not None:
            self.messages = compaction.messages
            self._refresh_system_prompt(self._current_query)
        with llm_operation(f"team-{self.role.value.lower()}"):
            raw = cancellable_call(
                lambda: self.llm_client.chat(self.messages, tools=tools),
                cancellation_event,
            )
        result = normalize_chat_result(
            raw,
            client=self.llm_client,
            messages=self.messages,
            tools=tools,
        )
        if self.memory_manager:
            self.memory_manager.token_budget.record_usage(
                result.usage.input_tokens,
                result.usage.output_tokens,
            )
        return result.message
