from __future__ import annotations

import json
import threading
import time
from collections.abc import Callable
from datetime import datetime
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


DEFAULT_SYSTEM_PROMPT = """You are StellarCode, a small coding agent.

You can answer directly or call tools when you need local file or command context.
Use tools only when they help. After receiving tool results, continue reasoning and
produce a concise final answer for the user.

Use list_dir to inspect a directory and delete_file to delete a file. Do not claim that
a filesystem operation succeeded until its tool result confirms success.
File tools accept absolute paths and may access locations outside the working directory.
Tools whose names start with mcp__ come from configured third-party MCP servers. Use
their descriptions and JSON schemas like built-in tools; do not invent MCP tool names.
Use web_search for current, recent, or uncertain public information. Use web_fetch when
the user provides a known HTTP or HTTPS URL, or after web_search identifies a page that
needs deeper reading. Cite the result URLs in the final answer when web tools are used.
For a normal public page with a known URL, try web_fetch once because it is cheaper and
returns clean text. If web_fetch fails, returns an empty or blocked shell, or the page
needs JavaScript, interaction, forms, console logs, or network inspection, use the
available chrome-devtools MCP tools. Sites known to resist static fetching, including
WeChat article pages, may go directly to chrome-devtools. Navigate or open the page,
wait for the needed content, then prefer mcp__chrome-devtools__take_snapshot for readable
DOM text. Use take_screenshot only when the user explicitly requests an image or visual
inspection. Prefer fill_form for multiple fields and wait_for for asynchronous content.
Chrome starts in isolated mode and cannot see the user's existing cookies or tabs. If a
task genuinely requires an existing login session, call browser_status and then
browser_connect. After connecting, use browser_tabs or list_pages to select the relevant
tab. Call browser_disconnect when shared access is no longer needed. In shared mode,
close_page may only close a tab opened by StellarCode during the current shared session.
Private repositories and other authenticated pages must use the shared browser session.
If browser_connect fails, stop browser work and report its exact setup error. Do not fall
back to web_fetch, git, gh, or execute_command unless the user explicitly asks to use
separately configured command-line credentials.
For identity, profile, birthday, membership, and other factual questions, search once,
then inspect Search quality and Fetch guidance. Fetch at most three relevant pages when
the snippets are insufficient or need verification. A low-quality search is not evidence
that the requested fact is unavailable. Do not keep inventing slightly different queries:
use the fallback results, fetch the best candidates, or explain the remaining uncertainty.
Do not repeat a successful search or fetch unless the user asks for a refresh.
When several tool calls are independent, return them together in one response so they
can run in parallel. Keep dependent tool calls in separate rounds.
Avoid full-disk recursive scans. Narrow exploration with list_dir, read_file, and
search_code instead.
User messages may contain @image:<path>, @image:"path with spaces", or @clipboard.
When an image attachment is present, inspect the actual image instead of inferring its
contents from the filename or prior context. Tool-returned images arrive in a following
user-role attachment message for API compatibility.
Desktop user messages may also contain @skill:<name> and
@mcp:<mcp__server__tool> references. An explicitly referenced enabled Skill is loaded
into that same task. Treat an MCP reference as a preference for that exact discovered
tool when relevant, not as permission to call it blindly or bypass approval and policy.

Follow the retrieval policy stated in the search_code tool description. When automatic
retrieval is enabled, use search_code for questions about how the current codebase works
before answering; when it is disabled, only use RAG on the user's explicit request. Use
read_file when exact surrounding source is needed after retrieval.
"""


class ChatClient(Protocol):
    def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        temperature: float = 0.2,
        on_delta: Callable[[str], None] | None = None,
    ) -> ChatResult | dict[str, Any]:
        ...


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
        history_summary: str = "",
        history_compaction_count: int = 0,
        history_last_compacted_at: str | None = None,
        llm_operation_name: str = "react",
        stream_output: bool = True,
    ) -> None:
        if max_web_search_calls < 1:
            raise ValueError("max_web_search_calls must be at least 1.")
        self.llm_client = llm_client
        self.tool_registry = tool_registry
        self.max_iterations = max_iterations
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
        self.history_compactor = ConversationHistoryCompactor(
            context_window=context_window,
            summary=history_summary,
            compaction_count=history_compaction_count,
            last_compacted_at=history_last_compacted_at,
        )
        self.messages: list[dict[str, Any]] = [{"role": "system", "content": system_prompt}]

    def reset(self) -> None:
        self.messages = [{"role": "system", "content": self.base_system_prompt}]
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
        self._refresh_system_prompt(user_input)

        self.messages.append(
            self.image_parser.user_message(self._prepend_skill_bodies(user_input))
        )
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
        self._refresh_system_prompt(original_input)
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
            try:
                assistant_message = self._chat(
                    self.tool_registry.schemas(),
                    cancellation_event,
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
            self._checkpoint("tool_results")
            raise_if_cancelled(cancellation_event)
            if repeated_failure_result is not None:
                return self._finish_after_repeated_failure(
                    repeated_failure_result,
                    cancellation_event,
                )

        return self._finish_after_iteration_limit(execution_trace, cancellation_event)

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

    def _refresh_system_prompt(self, query: str) -> None:
        content = f"{self.base_system_prompt}\n\n{runtime_context()}"
        if self.skill_registry:
            skill_index = format_skill_index(self.skill_registry.enabled_skills())
            if skill_index:
                content = f"{content}\n\n{skill_index}"
        if self.memory_manager:
            memory_context = self.memory_manager.build_context_for_query(query)
            if memory_context:
                content = f"{content}\n\n{memory_context}"
        self.messages[0] = {
            "role": "system",
            "content": self.history_compactor.decorate_system_prompt(content),
        }

    def _prepend_skill_bodies(self, user_input: str) -> str:
        if not self.skill_context_buffer:
            return user_input
        loaded = self.skill_context_buffer.drain()
        if not loaded:
            return user_input
        return f"{loaded}\n用户输入：\n{user_input}"

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
            assistant_message = self._chat(None, cancellation_event)
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
            assistant_message = self._chat(None, cancellation_event)
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
            final_error = (
                f"Final answer request failed: {type(exc).__name__}: {exc}"
            )

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
    ) -> dict[str, Any]:
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
                self._refresh_system_prompt(self._current_query)
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
            self._emit_event("assistant.delta", {"text": text})

        def collect_delta(text: str) -> None:
            nonlocal pending_chars
            if (
                not text
                or cancellation_event is not None
                and cancellation_event.is_set()
            ):
                return
            pending_delta.append(text)
            pending_chars += len(text)
            if pending_chars >= 48 or time.monotonic() - last_flush >= 0.05:
                flush_delta()

        delta_callback = (
            collect_delta if self.stream_output and self.event_callback is not None else None
        )
        try:
            with llm_operation(self.llm_operation_name):
                raw = cancellable_call(
                    lambda: chat_with_optional_delta(
                        self.llm_client,
                        self.messages,
                        tools=tools,
                        temperature=0.2,
                        on_delta=delta_callback,
                    ),
                    cancellation_event,
                )
        finally:
            flush_delta()
        result = normalize_chat_result(
            raw,
            client=self.llm_client,
            messages=self.messages,
            tools=tools,
        )
        if emitted_delta and result.message.get("tool_calls"):
            # A model may emit a short preamble before requesting tools. Remove that
            # provisional text so the following final-answer stream does not append to it.
            self._emit_event("assistant.delta", {"text": "", "reset": True})
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
        (
            "Reason: the model requested tools in every round and did not produce "
            "a final answer."
        ),
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
        metadata[key]
        for key in ("Search provider", "Results", "Search quality")
        if key in metadata
    ]
    return f" ({', '.join(values)})" if values else ""


def runtime_context(now: datetime | None = None) -> str:
    local_now = now.astimezone() if now is not None else datetime.now().astimezone()
    timezone_name = local_now.tzname() or str(local_now.utcoffset() or "local")
    return (
        "Runtime context:\n"
        f"- Current local date: {local_now.date().isoformat()}\n"
        f"- Current local time: {local_now.strftime('%H:%M:%S')} ({timezone_name})\n"
        "- Treat words such as today, latest, current, and recently relative to this date."
    )
