"""ReAct execution loop and the prompt layers sent to an LLM provider.

This module owns one conversation's live ``messages`` list.  Runtime persistence,
task routing, and workspace isolation deliberately live elsewhere so Agent remains the
single place that translates model tool calls into ToolRegistry executions.
"""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol

from stellarcode.cancellation import (
    TaskCancelledError,
    cancellable_call,
    raise_if_cancelled,
)
from stellarcode.image import (
    ImageReferenceParser,
    image_tool_message,
    prune_historical_images,
)
from stellarcode.llm.types import (
    ChatResult,
    chat_with_optional_delta,
    llm_operation,
    normalize_chat_result,
)
from stellarcode.llm.message_history import repair_tool_message_history
from stellarcode.memory import ConversationHistoryCompactor, MemoryManager
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
)
from stellarcode.skill import (
    SkillContextBuffer,
    SkillRegistry,
    activate_skill_context,
    format_skill_index,
)
from stellarcode.tools.registry import (
    ToolExecutionResult,
    ToolInvocation,
    ToolRegistry,
)


DEFAULT_SYSTEM_PROMPT = """## Execution role

Fulfill the user's request directly when no workspace evidence or action is needed.
Otherwise inspect, act, and verify iteratively with the available tools. Continue from
tool results until the requested outcome is complete, a policy decision stops the action,
or a concrete blocker requires user input. Do not stop after merely describing what could
be done.
"""


class ChatClient(Protocol):
    def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        temperature: float = 0.2,
        on_delta: Callable[[str], None] | None = None,
    ) -> ChatResult | dict[str, Any]: ...


class Agent:
    def __init__(
        self,
        llm_client: ChatClient,
        tool_registry: ToolRegistry,
        system_prompt: str = DEFAULT_SYSTEM_PROMPT,
        max_iterations: int = 8,
        memory_manager: MemoryManager | None = None,
        progress_callback: Callable[[str], None] | None = None,
        event_callback: Callable[[str, dict[str, Any]], None] | None = None,
        checkpoint_callback: Callable[[str], None] | None = None,
        max_web_search_calls: int = 4,
        skill_registry: SkillRegistry | None = None,
        skill_context_buffer: SkillContextBuffer | None = None,
        workspace: str | Path | None = None,
        context_window: int = 200_000,
        rag_auto_retrieval: bool | None = True,
        history_summary: str = "",
        history_compaction_count: int = 0,
        history_last_compacted_at: str | None = None,
        llm_operation_name: str = "react",
        stream_output: bool = True,
        delta_event_type: str = "assistant.delta",
        delta_event_data: dict[str, Any] | None = None,
        prompt_mode: PromptMode = PromptMode.REACT,
        prompt_assembler: PromptAssembler | None = None,
        temperature: float = 0.2,
    ) -> None:
        if max_web_search_calls < 1:
            raise ValueError("max_web_search_calls must be at least 1.")
        if not 0.0 <= temperature <= 2.0:
            raise ValueError("temperature must be between 0 and 2.")
        self.llm_client = llm_client
        self.tool_registry = tool_registry
        self.max_iterations = max_iterations
        self.temperature = temperature
        self.base_system_prompt = system_prompt
        self.memory_manager = memory_manager
        self.progress_callback = progress_callback
        self.event_callback = event_callback
        self.checkpoint_callback = checkpoint_callback
        self.max_web_search_calls = max_web_search_calls
        self.skill_registry = skill_registry
        self.skill_context_buffer = skill_context_buffer
        self.workspace = Path(workspace or ".").resolve()
        self.image_parser = ImageReferenceParser(self.workspace)
        self._web_search_calls = 0
        self._current_query = ""
        self.llm_operation_name = llm_operation_name
        self.stream_output = stream_output
        # Plan mode reuses the provider streaming path but routes each step's
        # visible text to its own plan card instead of the main assistant bubble.
        self.delta_event_type = delta_event_type
        self.delta_event_data = dict(delta_event_data or {})
        self.prompt_mode = prompt_mode
        self.prompt_assembler = prompt_assembler or PromptAssembler()
        self._current_memory_context = ""
        self._last_prompt_snapshot: PromptSnapshot | None = None
        self.rag_auto_retrieval = rag_auto_retrieval
        self.history_compactor = ConversationHistoryCompactor(
            context_window=context_window,
            summary=history_summary,
            compaction_count=history_compaction_count,
            last_compacted_at=history_last_compacted_at,
        )
        self.messages: list[dict[str, Any]] = [{"role": "system", "content": system_prompt}]

    def reset(self) -> None:
        self.messages = [{"role": "system", "content": self.base_system_prompt}]
        self._current_memory_context = ""
        self._last_prompt_snapshot = None
        self.history_compactor.reset()
        if self.skill_context_buffer:
            self.skill_context_buffer.clear()

    def run(
        self,
        user_input: str,
        cancellation_event: threading.Event | None = None,
    ) -> str:
        raise_if_cancelled(cancellation_event)
        self._current_query = user_input
        self._web_search_calls = 0
        prune_historical_images(self.messages)
        if self.memory_manager:
            self.memory_manager.add_user_message(user_input)
        self._refresh_system_prompt(user_input, include_memory_context=True)

        self.messages.append(self.image_parser.user_message(self._prepend_skill_bodies(user_input)))
        self._checkpoint("user_message")
        return self._run_iterations(cancellation_event)

    def resume(
        self,
        original_input: str,
        cancellation_event: threading.Event | None = None,
    ) -> str:
        """Continue a durable ReAct history without repeating the user's turn."""

        raise_if_cancelled(cancellation_event)
        self._current_query = original_input
        self._web_search_calls = 0
        prune_historical_images(self.messages)
        self.messages, _ = repair_tool_message_history(self.messages)
        self._refresh_system_prompt(original_input, include_memory_context=True)
        self.messages.append(
            {
                "role": "user",
                "content": (
                    "[RUNTIME_RECOVERY] The desktop Sidecar restarted while this task "
                    "was running. Continue the original request from the durable history. "
                    "An interrupted tool result means its side effects are unknown: inspect "
                    "the current workspace or system state before deciding whether to retry. "
                    "Do not repeat an already confirmed operation."
                ),
            }
        )
        self._checkpoint("recovery_ready")
        return self._run_iterations(cancellation_event)

    def _run_iterations(
        self,
        cancellation_event: threading.Event | None,
    ) -> str:
        repeated_failures: dict[tuple[str, str, str], int] = {}
        execution_trace: list[tuple[ToolInvocation, ToolExecutionResult]] = []

        for iteration in range(1, self.max_iterations + 1):
            raise_if_cancelled(cancellation_event)
            # One registry snapshot drives both the prompt's capability policy and the
            # provider schemas. Dynamic MCP/browser registration can therefore take
            # effect on the next model round without the two views drifting apart.
            tool_definitions = self.tool_registry.list_tools()
            available_tools = frozenset(tool.name for tool in tool_definitions)
            try:
                assistant_message = self._chat(
                    [tool.to_openai_tool() for tool in tool_definitions],
                    cancellation_event,
                    available_tools=available_tools,
                )
            except TaskCancelledError:
                raise
            except Exception as exc:
                content = (
                    f"LLM request failed on iteration {iteration}/{self.max_iterations}: "
                    f"{type(exc).__name__}: {exc}"
                )
                self._emit_progress(f"[Agent] {content}")
                self._record_final_answer(content)
                return content
            self.messages.append(assistant_message)
            self._checkpoint("assistant_message")

            tool_calls = assistant_message.get("tool_calls") or []
            if not tool_calls:
                content = str(assistant_message.get("content") or "")
                self._record_final_answer(content)
                return content

            tool_results, execution_results = self._execute_tool_calls(
                tool_calls,
                iteration,
                cancellation_event,
            )
            execution_trace.extend(
                zip(
                    [_tool_invocation(tool_call) for tool_call in tool_calls],
                    execution_results,
                )
            )
            repeated_failure_result: dict[str, Any] | None = None
            for tool_call, tool_result in zip(tool_calls, tool_results):
                self.messages.append(tool_result)
                if self.memory_manager:
                    self.memory_manager.add_tool_result(
                        tool_result["name"],
                        tool_result["content"],
                    )

                if _is_failed_tool_result(tool_result["content"]):
                    fingerprint = _tool_failure_fingerprint(tool_call, tool_result)
                    repeated_failures[fingerprint] = repeated_failures.get(fingerprint, 0) + 1
                    if repeated_failures[fingerprint] >= 2:
                        repeated_failure_result = tool_result
            self._append_tool_images(execution_results)
            self._append_loaded_skill_context()
            self._checkpoint("tool_results")
            raise_if_cancelled(cancellation_event)
            if repeated_failure_result is not None:
                return self._finish_after_repeated_failure(
                    repeated_failure_result,
                    cancellation_event,
                )

        return self._finish_after_iteration_limit(execution_trace, cancellation_event)

    
    """
    负责处理模型这一轮返回的全部 tool_calls。它会生成 UI 的 tool.started 事件、
    做 web_search 次数限制、调用批量执行、再把每个结果转换成role=tool 消息回填给模型。  
    """

    def _execute_tool_calls(
        self,
        tool_calls: list[dict[str, Any]],
        iteration: int,
        cancellation_event: threading.Event | None = None,
    ) -> tuple[list[dict[str, Any]], list[ToolExecutionResult]]:
        invocations = [_tool_invocation(tool_call) for tool_call in tool_calls]
        for invocation in invocations:
            change_preview = self.tool_registry.preview(
                invocation.name,
                invocation.arguments,
            )
            event_data = {
                "tool_call_id": invocation.id,
                "name": invocation.name,
                "arguments": self.tool_registry.event_arguments(
                    invocation.name,
                    invocation.arguments,
                ),
                "iteration": iteration,
            }
            if change_preview is not None:
                event_data["change_preview"] = change_preview
            self._emit_event(
                "tool.started",
                event_data,
            )
            self._emit_progress(
                f"[Agent {iteration}/{self.max_iterations}] calling "
                f"{invocation.name} {_format_tool_arguments(invocation.arguments)}"
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
                            f"({self.max_web_search_calls} per task). Use relevant URLs "
                            "from earlier results with web_fetch, or answer from the "
                            "evidence already collected."
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
            failed = not result.success or _is_failed_tool_result(result.result)
            if failed:
                self._emit_event(
                    "tool.failed",
                    {
                        "tool_call_id": result.id,
                        "name": result.name,
                        "error": _truncate_text(result.result, 2000),
                        "elapsed_ms": result.elapsed_ms,
                        "timed_out": result.timed_out,
                    },
                )
                self._emit_progress(
                    f"[Tool {result.name}] failed after {result.elapsed_ms} ms: "
                    f"{_truncate_text(result.result, 500)}"
                )
            else:
                self._emit_event(
                    "tool.completed",
                    {
                        "tool_call_id": result.id,
                        "name": result.name,
                        "result_preview": _truncate_text(result.result, 2000),
                        "elapsed_ms": result.elapsed_ms,
                        "success": True,
                        "has_attachments": bool(result.image_parts),
                    },
                )
                self._emit_progress(
                    f"[Tool {result.name}] completed in {result.elapsed_ms} ms"
                    f"{_tool_result_summary(result)}"
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
        return messages, results

    def _append_tool_images(self, results: list[ToolExecutionResult]) -> None:
        for result in results:
            message = image_tool_message(result.name, result.image_parts)
            if message is not None:
                self.messages.append(message)

    def _refresh_system_prompt(
        self,
        query: str,
        *,
        available_tools: frozenset[str] | None = None,
        include_memory_context: bool = False,
        publish_snapshot: bool = True,
    ) -> None:
        # Rebuild instead of append. Stable policies remain in the system role;
        # query-sensitive Memory is replaced separately as untrusted user context.
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
                    else frozenset(tool.name for tool in self.tool_registry.list_tools())
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
            self.messages.extend(assembly.context_messages)

    def prompt_snapshot(self, *, include_sensitive: bool = False) -> dict[str, Any]:
        """Return the current ReAct prompt preview without exposing Memory by default."""

        if self._last_prompt_snapshot is None:
            self._refresh_system_prompt("", publish_snapshot=False)
        assert self._last_prompt_snapshot is not None
        return self._last_prompt_snapshot.to_dict(include_sensitive=include_sensitive)

    def _prepend_skill_bodies(self, user_input: str) -> str:
        if not self.skill_context_buffer:
            return user_input
        loaded = self.skill_context_buffer.drain()
        if not loaded:
            return user_input
        return f"{loaded}\n用户输入：\n{user_input}"

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
                    "Use this guidance for the current task. Continue from the tool results "
                    "above without asking the user to repeat the request."
                ),
            }
        )

    def _finish_after_repeated_failure(
        self,
        tool_result: dict[str, Any],
        cancellation_event: threading.Event | None = None,
    ) -> str:
        self._emit_progress(
            "[Agent] The same tool call failed twice; requesting a final explanation."
        )
        self.messages.append(
            {
                "role": "user",
                "content": (
                    "The same tool call failed twice. Do not call another tool now. "
                    "Explain the failure and the concrete next step to the user."
                ),
            }
        )
        self._checkpoint("repeated_failure_prompt")
        try:
            assistant_message = self._chat(
                None,
                cancellation_event,
                available_tools=frozenset(),
            )
        except TaskCancelledError:
            raise
        except Exception as exc:
            content = f"Tool operation failed repeatedly. {tool_result['content']}"
            self._emit_progress(
                f"[Agent] Final explanation request failed: {type(exc).__name__}: {exc}"
            )
            self._record_final_answer(content)
            return content
        self.messages.append(assistant_message)
        self._checkpoint("assistant_final")
        content = str(assistant_message.get("content") or "").strip()
        if not content:
            content = f"Tool operation failed repeatedly. {tool_result['content']}"
        self._record_final_answer(content)
        return content

    def _finish_after_iteration_limit(
        self,
        execution_trace: list[tuple[ToolInvocation, ToolExecutionResult]],
        cancellation_event: threading.Event | None = None,
    ) -> str:
        self._emit_progress(
            f"[Agent] Reached the {self.max_iterations}-round tool limit; "
            "requesting a final answer with tools disabled."
        )
        self.messages.append(
            {
                "role": "user",
                "content": (
                    f"You have reached the tool-call limit of {self.max_iterations} rounds. "
                    "Do not call any more tools. Using the tool results already available, "
                    "answer the user's original request now. If the task cannot be completed, "
                    "state the exact reason and the last relevant tool error."
                ),
            }
        )
        self._checkpoint("iteration_limit_prompt")
        final_error = ""
        try:
            assistant_message = self._chat(
                None,
                cancellation_event,
                available_tools=frozenset(),
            )
            self.messages.append(assistant_message)
            self._checkpoint("assistant_final")
            content = str(assistant_message.get("content") or "").strip()
            if content:
                self._record_final_answer(content)
                return content
            final_error = "The model returned no final text even with tools disabled."
        except TaskCancelledError:
            raise
        except Exception as exc:
            final_error = f"Final answer request failed: {type(exc).__name__}: {exc}"

        content = _iteration_limit_diagnostic(
            self.max_iterations,
            execution_trace,
            final_error,
        )
        self._emit_progress(f"[Agent] {final_error}")
        self._record_final_answer(content)
        return content

    def _emit_progress(self, message: str) -> None:
        if not self.progress_callback:
            return
        try:
            self.progress_callback(message)
        except Exception:
            pass

    def _emit_event(self, event_type: str, data: dict[str, Any]) -> None:
        if not self.event_callback:
            return
        try:
            self.event_callback(event_type, data)
        except Exception:
            pass

    def _checkpoint(self, stage: str) -> None:
        if not self.checkpoint_callback:
            return
        try:
            self.checkpoint_callback(stage)
        except Exception:
            # A checkpoint is a recovery aid; an I/O failure must not mutate the
            # in-memory message list halfway through the active turn.
            pass

    def _record_final_answer(self, content: str) -> None:
        if not self.memory_manager:
            return
        self.memory_manager.add_assistant_message(content)

    def history_snapshot(self) -> dict[str, Any]:
        return self.history_compactor.snapshot()

    def _chat(
        self,
        tools: list[dict[str, Any]] | None,
        cancellation_event: threading.Event | None,
        *,
        available_tools: frozenset[str] | None = None,
    ) -> dict[str, Any]:
        if available_tools is not None:
            self._refresh_system_prompt(
                self._current_query,
                available_tools=available_tools,
            )
        # Message-history compaction is distinct from long-term memory.  It
        # shrinks the exact provider payload while preserving tool boundaries.
        compaction_needed = self.history_compactor.needs_compaction(self.messages, tools)
        compaction = None
        if compaction_needed:
            self._emit_event(
                "history.compaction.started",
                {
                    "estimated_tokens": self.history_compactor.estimated_tokens(
                        self.messages,
                        tools,
                    ),
                    "trigger_tokens": self.history_compactor.trigger_tokens,
                },
            )
        try:
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
                self._checkpoint("history_compacted")
                self._emit_event(
                    "history.compacted",
                    {
                        "before_tokens": compaction.before_tokens,
                        "after_tokens": compaction.after_tokens,
                        "compacted_turns": compaction.compacted_turns,
                        "method": compaction.method,
                        "compaction_count": compaction.compaction_count,
                    },
                )
        finally:
            if compaction_needed:
                self._emit_event(
                    "history.compaction.finished",
                    {"compacted": compaction is not None},
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
            self._emit_event(
                self.delta_event_type,
                {**self.delta_event_data, "text": text},
            )

        def collect_delta(text: str) -> None:
            nonlocal pending_chars
            if not text or cancellation_event is not None and cancellation_event.is_set():
                return
            pending_delta.append(text)
            pending_chars += len(text)
            if pending_chars >= 48 or time.monotonic() - last_flush >= 0.05:
                flush_delta()

        delta_callback = (
            collect_delta if self.stream_output and self.event_callback is not None else None
        )
        try:
            provider_messages = strip_internal_context_metadata(self.messages)
            with llm_operation(self.llm_operation_name):
                raw = cancellable_call(
                    lambda: chat_with_optional_delta(
                        self.llm_client,
                        provider_messages,
                        tools=tools,
                        temperature=self.temperature,
                        on_delta=delta_callback,
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
            # A model may emit a short preamble before requesting tools. Remove that
            # provisional text so the following final-answer stream does not append to it.
            self._emit_event(
                self.delta_event_type,
                {**self.delta_event_data, "text": "", "reset": True},
            )
        if self.memory_manager:
            self.memory_manager.token_budget.record_usage(
                result.usage.input_tokens,
                result.usage.output_tokens,
            )
        return result.message


def _tool_failure_fingerprint(
    tool_call: dict[str, Any],
    tool_result: dict[str, Any],
) -> tuple[str, str, str]:
    function = tool_call.get("function") or {}
    arguments = function.get("arguments")
    if isinstance(arguments, dict):
        serialized = json.dumps(arguments, ensure_ascii=False, sort_keys=True)
    else:
        serialized = str(arguments or "")
    return (
        str(function.get("name") or ""),
        serialized,
        str(tool_result.get("content") or ""),
    )


def _is_failed_tool_result(content: str) -> bool:
    if content.startswith(
        (
            "Tool error:",
            "[HITL] Operation rejected:",
            "[HITL] Operation skipped",
            "[POLICY]",
            "[WEB_POLICY]",
        )
    ):
        return True
    if content.startswith("exit_code:"):
        first_line = content.splitlines()[0]
        return first_line.strip() != "exit_code: 0"
    return False


def _tool_invocation(tool_call: dict[str, Any]) -> ToolInvocation:
    function = tool_call.get("function") or {}
    return ToolInvocation(
        id=str(tool_call.get("id") or "unknown_tool_call"),
        name=str(function.get("name") or "unknown_tool"),
        arguments=function.get("arguments"),
    )


def _format_tool_arguments(
    arguments: str | dict[str, Any] | None,
    max_chars: int = 240,
) -> str:
    if arguments is None or arguments == "":
        return "{}"
    if isinstance(arguments, dict):
        rendered = json.dumps(arguments, ensure_ascii=False, separators=(",", ":"))
    else:
        try:
            parsed = json.loads(arguments)
            rendered = json.dumps(parsed, ensure_ascii=False, separators=(",", ":"))
        except (json.JSONDecodeError, TypeError):
            rendered = str(arguments)
    return _truncate_text(rendered, max_chars)


def _arguments_object(arguments: str | dict[str, Any] | None) -> dict[str, Any]:
    if isinstance(arguments, dict):
        return arguments
    if not arguments:
        return {}
    try:
        parsed = json.loads(arguments)
    except (json.JSONDecodeError, TypeError):
        return {"value": str(arguments)}
    return parsed if isinstance(parsed, dict) else {"value": parsed}


def _iteration_limit_diagnostic(
    max_iterations: int,
    execution_trace: list[tuple[ToolInvocation, ToolExecutionResult]],
    final_error: str,
) -> str:
    lines = [
        f"Agent could not finish after {max_iterations} tool-call rounds.",
        ("Reason: the model requested tools in every round and did not produce a final answer."),
    ]
    if final_error:
        lines.append(final_error)
    if execution_trace:
        invocation, result = execution_trace[-1]
        failed = not result.success or _is_failed_tool_result(result.result)
        lines.extend(
            [
                f"Last tool: {invocation.name} {_format_tool_arguments(invocation.arguments)}",
                f"Last tool status: {'failed' if failed else 'succeeded'}",
                f"Last tool result: {_truncate_text(result.result, 800)}",
            ]
        )
    else:
        lines.append("No tool result was recorded.")
    return "\n".join(lines)


def _truncate_text(value: str, max_chars: int) -> str:
    compact = " ".join(str(value).split())
    if len(compact) <= max_chars:
        return compact
    return f"{compact[: max_chars - 16]}...[truncated]"


def _tool_result_summary(result: ToolExecutionResult) -> str:
    if result.name != "web_search":
        return ""
    metadata: dict[str, str] = {}
    for line in result.result.splitlines():
        if ": " not in line:
            continue
        key, value = line.split(": ", 1)
        if key in {"Search provider", "Results", "Search quality"}:
            metadata[key] = value
    values = [
        metadata[key] for key in ("Search provider", "Results", "Search quality") if key in metadata
    ]
    return f" ({', '.join(values)})" if values else ""
