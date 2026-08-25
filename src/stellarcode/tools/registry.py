"""Shared tool catalog and bounded parallel execution engine.

All Agent modes use this registry so schema generation, cancellation, tracing, timeout
handling, and ordered result assembly have one implementation.
"""

from __future__ import annotations

import contextvars
import json
import queue
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from stellarcode.llm.types import current_llm_scope
from stellarcode.trace import TraceRecorder, TraceTarget


class _CombinedCancellation:
    def __init__(
        self,
        *,
        batch_event: threading.Event | None = None,
        task_event: threading.Event | None = None,
    ) -> None:
        self._batch_event = batch_event
        self._task_event = task_event

    def is_set(self) -> bool:
        return bool(
            (self._batch_event is not None and self._batch_event.is_set())
            or (self._task_event is not None and self._task_event.is_set())
        )

    def reason(self) -> str | None:
        if self._task_event is not None and self._task_event.is_set():
            return "task"
        if self._batch_event is not None and self._batch_event.is_set():
            return "batch"
        return None


_TOOL_CANCELLATION_EVENT: contextvars.ContextVar[threading.Event | _CombinedCancellation | None] = (
    contextvars.ContextVar("stellarcode_tool_cancellation_event", default=None)
)
_CURRENT_TRACE_TARGET = object()


def tool_cancellation_requested() -> bool:
    """Return whether the active parallel tool batch cancelled this invocation."""

    event = _TOOL_CANCELLATION_EVENT.get()
    return event is not None and event.is_set()


def tool_cancellation_reason() -> str | None:
    """Return whether cancellation came from the task or the tool batch."""

    event = _TOOL_CANCELLATION_EVENT.get()
    if event is None or not event.is_set():
        return None
    if isinstance(event, _CombinedCancellation):
        return event.reason()
    return "batch"


class ToolExecutionError(RuntimeError):
    """Raised when a tool cannot be executed."""


@dataclass(frozen=True)
class ToolDefinition:
    name: str
    description: str
    parameters: dict[str, Any]
    handler: Callable[..., str | ToolOutput]
    previewer: Callable[..., dict[str, Any]] | None = None

    def to_openai_tool(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


@dataclass(frozen=True)
class ToolInvocation:
    id: str
    name: str
    arguments: str | dict[str, Any] | None = None


@dataclass(frozen=True)
class ToolOutput:
    text: str
    image_parts: tuple[dict[str, Any], ...] = ()
    trace_text: str | None = None


@dataclass(frozen=True)
class ToolExecutionResult:
    id: str
    name: str
    arguments: str | dict[str, Any] | None
    result: str
    elapsed_ms: int
    success: bool = True
    timed_out: bool = False
    image_parts: tuple[dict[str, Any], ...] = ()


class ToolRegistry:
    def __init__(
        self,
        max_parallel_tools: int = 4,
        batch_timeout_seconds: float = 90,
        trace_recorder: TraceRecorder | None = None,
    ) -> None:
        if max_parallel_tools < 1:
            raise ValueError("max_parallel_tools must be at least 1.")
        if batch_timeout_seconds <= 0:
            raise ValueError("batch_timeout_seconds must be greater than 0.")
        self._tools: dict[str, ToolDefinition] = {}
        self._tools_lock = threading.RLock()
        self._executions_condition = threading.Condition(threading.RLock())
        self._active_executions = 0
        self._active_executions_by_task: dict[str, int] = {}
        self.max_parallel_tools = max_parallel_tools
        self.batch_timeout_seconds = batch_timeout_seconds
        self.trace_recorder = trace_recorder

    def register(self, tool: ToolDefinition) -> None:
        with self._tools_lock:
            if tool.name in self._tools:
                raise ValueError(f"Tool already registered: {tool.name}")
            self._tools[tool.name] = tool

    def unregister(self, name: str) -> None:
        with self._tools_lock:
            self._tools.pop(name, None)

    def list_tools(self) -> list[ToolDefinition]:
        with self._tools_lock:
            return list(self._tools.values())

    def schemas(self) -> list[dict[str, Any]]:
        return [tool.to_openai_tool() for tool in self.list_tools()]

    def preview(
        self,
        name: str,
        arguments: str | dict[str, Any] | None,
    ) -> dict[str, Any] | None:
        with self._tools_lock:
            tool = self._tools.get(name)
        if tool is None or tool.previewer is None:
            return None
        try:
            kwargs = {
                key: value
                for key, value in self._parse_arguments(arguments).items()
                if not key.startswith("__")
            }
            return tool.previewer(**kwargs)
        except Exception as exc:
            return {
                "operation": "unknown",
                "path": "",
                "workspace_scoped": False,
                "rollback_protected": False,
                "protection_reason": "preview_error",
                "sensitive": False,
                "binary": False,
                "before_sha256": None,
                "after_sha256": None,
                "additions": 0,
                "deletions": 0,
                "diff": "",
                "truncated": False,
                "error": f"{type(exc).__name__}: {exc}",
            }

    def event_arguments(
        self,
        name: str,
        arguments: str | dict[str, Any] | None,
    ) -> dict[str, Any]:
        """Return journal-safe arguments without embedding complete replacement files."""

        try:
            parsed = {
                key: value
                for key, value in self._parse_arguments(arguments).items()
                if not key.startswith("__")
            }
        except ToolExecutionError:
            return {"value": str(arguments or "")[:2_000]}
        return _sanitize_file_mutation_arguments(name, parsed)

    @staticmethod
    def sanitize_event_arguments(
        name: str,
        arguments: str | dict[str, Any] | None,
    ) -> dict[str, Any]:
        """Sanitize arguments without requiring a registered tool definition."""

        try:
            parsed = ToolRegistry._parse_arguments(arguments)
        except ToolExecutionError:
            return {"value": str(arguments or "")[:2_000]}
        safe = {key: value for key, value in parsed.items() if not key.startswith("__")}
        return _sanitize_file_mutation_arguments(name, safe)

    def execute(self, name: str, arguments: str | dict[str, Any] | None) -> str:
        started_at = time.monotonic()
        trace_target = (
            self.trace_recorder.capture_target() if self.trace_recorder else None
        )
        try:
            output = self._execute_output(name, arguments)
        except ToolExecutionError as exc:
            self._record_tool_result(
                tool_call_id="direct",
                name=name,
                arguments=arguments,
                result=f"Tool error: {exc}",
                elapsed_ms=int((time.monotonic() - started_at) * 1000),
                success=False,
                trace_target=trace_target,
            )
            raise
        self._record_tool_result(
            tool_call_id="direct",
            name=name,
            arguments=arguments,
            result=output.trace_text if output.trace_text is not None else output.text,
            elapsed_ms=int((time.monotonic() - started_at) * 1000),
            success=True,
            image_parts=output.image_parts,
            trace_target=trace_target,
        )
        return output.text

    def _execute_output(
        self,
        name: str,
        arguments: str | dict[str, Any] | None,
    ) -> ToolOutput:
        with self._tools_lock:
            tool = self._tools.get(name)
        if tool is None:
            raise ToolExecutionError(f"Unknown tool: {name}")

        kwargs = self._parse_arguments(arguments)
        execution_task_id = self._register_execution()
        try:
            try:
                result = tool.handler(**kwargs)
                if isinstance(result, ToolOutput):
                    return result
                return ToolOutput(str(result))
            except ToolExecutionError:
                raise
            except Exception as exc:
                raise ToolExecutionError(f"{name} failed: {exc}") from exc
        finally:
            self._finish_execution(execution_task_id)

    def _register_execution(self) -> str:
        _session_id, task_id = current_llm_scope()
        with self._executions_condition:
            self._active_executions += 1
            self._active_executions_by_task[task_id] = (
                self._active_executions_by_task.get(task_id, 0) + 1
            )
        return task_id

    def _finish_execution(self, task_id: str) -> None:
        with self._executions_condition:
            self._active_executions -= 1
            remaining = self._active_executions_by_task.get(task_id, 0) - 1
            if remaining > 0:
                self._active_executions_by_task[task_id] = remaining
            else:
                self._active_executions_by_task.pop(task_id, None)
            self._executions_condition.notify_all()

    def wait_for_quiescence(
        self,
        timeout_seconds: float | None = None,
        *,
        task_id: str | None = None,
    ) -> bool:
        """Wait until detached/cancelled tool handlers can no longer mutate state."""

        deadline = None if timeout_seconds is None else time.monotonic() + timeout_seconds
        with self._executions_condition:
            while (
                self._active_executions_by_task.get(task_id, 0)
                if task_id is not None
                else self._active_executions
            ):
                if deadline is None:
                    self._executions_condition.wait()
                    continue
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._executions_condition.wait(remaining)
            return True

    def execute_tools(
        self,
        invocations: list[ToolInvocation],
        timeout_seconds: float | None = None,
        cancellation_event: threading.Event | None = None,
    ) -> list[ToolExecutionResult]:
        if not invocations:
            return []
        if len(invocations) == 1:
            if cancellation_event is None:
                return [self._execute_invocation(invocations[0])]
            result_holder: list[ToolExecutionResult] = []
            execution_context = contextvars.copy_context()
            execution_task_id = self._register_execution()

            def run_single() -> None:
                try:
                    result_holder.append(
                        self._execute_invocation(
                            invocations[0],
                            cancellation_event=_CombinedCancellation(
                                task_event=cancellation_event,
                            ),
                        )
                    )
                finally:
                    self._finish_execution(execution_task_id)

            thread = threading.Thread(
                target=execution_context.run,
                args=(run_single,),
                name="stellarcode-tool-1",
                daemon=True,
            )
            thread.start()
            while thread.is_alive():
                thread.join(0.05)
                if result_holder:
                    return result_holder
                if cancellation_event.is_set():
                    return [
                        ToolExecutionResult(
                            id=invocations[0].id,
                            name=invocations[0].name,
                            arguments=invocations[0].arguments,
                            result="Tool error: Task cancelled by user.",
                            elapsed_ms=0,
                            success=False,
                        )
                    ]
            return [
                result_holder[0]
            ]

        batch_timeout = (
            self.batch_timeout_seconds if timeout_seconds is None else timeout_seconds
        )
        if batch_timeout <= 0:
            raise ValueError("timeout_seconds must be greater than 0.")

        # Workers consume in parallel, but results are stored at their original
        # indexes so the next LLM request receives tool messages in call order.
        # This is required by OpenAI-compatible tool-call protocols.
        work_queue: queue.Queue[tuple[int, ToolInvocation]] = queue.Queue()
        for index, invocation in enumerate(invocations):
            work_queue.put((index, invocation))

        results: list[ToolExecutionResult | None] = [None] * len(invocations)
        cancellation_events = [threading.Event() for _ in invocations]
        deadline = time.monotonic() + batch_timeout

        def run_worker(execution_task_id: str) -> None:
            try:
                while (
                    time.monotonic() < deadline
                    and not (cancellation_event and cancellation_event.is_set())
                ):
                    try:
                        index, invocation = work_queue.get_nowait()
                    except queue.Empty:
                        return
                    results[index] = self._execute_invocation(
                        invocation,
                        cancellation_event=_CombinedCancellation(
                            batch_event=cancellation_events[index],
                            task_event=cancellation_event,
                        ),
                    )
                    work_queue.task_done()
            finally:
                self._finish_execution(execution_task_id)

        threads = []
        for index in range(min(len(invocations), self.max_parallel_tools)):
            execution_context = contextvars.copy_context()
            execution_task_id = self._register_execution()
            threads.append(
                threading.Thread(
                    target=execution_context.run,
                    args=(run_worker, execution_task_id),
                    name=f"stellarcode-tool-{index + 1}",
                    daemon=True,
                )
            )
        for thread in threads:
            thread.start()
        while any(thread.is_alive() for thread in threads):
            if time.monotonic() >= deadline:
                break
            if cancellation_event is not None and cancellation_event.is_set():
                break
            for thread in threads:
                thread.join(min(0.05, max(0.0, deadline - time.monotonic())))

        # Do not wait indefinitely for an uncooperative tool process.  Mark its
        # individual cancellation token, synthesize an ordered error result, and
        # let the detached worker finish its own cleanup in the background.
        unfinished_indexes = {
            index for index, result in enumerate(results) if result is None
        }
        for index in unfinished_indexes:
            cancellation_events[index].set()

        task_cancelled = cancellation_event is not None and cancellation_event.is_set()
        timeout_ms = int(batch_timeout * 1000)
        completed_results = []
        for index, (invocation, result) in enumerate(zip(invocations, results)):
            if index not in unfinished_indexes and result is not None:
                completed_results.append(result)
                continue
            timeout_result = ToolExecutionResult(
                id=invocation.id,
                name=invocation.name,
                arguments=invocation.arguments,
                result=(
                    "Tool error: Task cancelled by user."
                    if task_cancelled
                    else f"Tool error: {invocation.name} timed out after "
                    f"{batch_timeout:g}s (batch timeout)."
                ),
                elapsed_ms=0 if task_cancelled else timeout_ms,
                success=False,
                timed_out=not task_cancelled,
            )
            self._record_tool_result(
                tool_call_id=invocation.id,
                name=invocation.name,
                arguments=invocation.arguments,
                result=timeout_result.result,
                elapsed_ms=timeout_result.elapsed_ms,
                success=False,
            )
            completed_results.append(timeout_result)
        return completed_results

    def _execute_invocation(
        self,
        invocation: ToolInvocation,
        cancellation_event: threading.Event | _CombinedCancellation | None = None,
    ) -> ToolExecutionResult:
        started_at = time.monotonic()
        trace_target = (
            self.trace_recorder.capture_target() if self.trace_recorder else None
        )
        if cancellation_event is not None and cancellation_event.is_set():
            result = "Tool error: Task cancelled by user."
            self._record_tool_result(
                tool_call_id=invocation.id,
                name=invocation.name,
                arguments=invocation.arguments,
                result=result,
                elapsed_ms=0,
                success=False,
                trace_target=trace_target,
            )
            return ToolExecutionResult(
                id=invocation.id,
                name=invocation.name,
                arguments=invocation.arguments,
                result=result,
                elapsed_ms=0,
                success=False,
            )
        cancellation_token = _TOOL_CANCELLATION_EVENT.set(cancellation_event)
        try:
            try:
                output = self._execute_output(invocation.name, invocation.arguments)
                if cancellation_event is not None and cancellation_event.is_set():
                    result = "Tool error: Task cancelled by user."
                    trace_result = result
                    image_parts = ()
                    success = False
                else:
                    result = output.text
                    trace_result = output.trace_text if output.trace_text is not None else result
                    image_parts = output.image_parts
                    success = True
            except ToolExecutionError as exc:
                result = f"Tool error: {exc}"
                trace_result = result
                image_parts = ()
                success = False
        finally:
            _TOOL_CANCELLATION_EVENT.reset(cancellation_token)
        elapsed_ms = int((time.monotonic() - started_at) * 1000)
        self._record_tool_result(
            tool_call_id=invocation.id,
            name=invocation.name,
            arguments=invocation.arguments,
            result=trace_result,
            elapsed_ms=elapsed_ms,
            success=success,
            image_parts=image_parts,
            trace_target=trace_target,
        )
        return ToolExecutionResult(
            id=invocation.id,
            name=invocation.name,
            arguments=invocation.arguments,
            result=result,
            elapsed_ms=elapsed_ms,
            success=success,
            image_parts=image_parts,
        )

    def _record_tool_result(
        self,
        *,
        tool_call_id: str,
        name: str,
        arguments: str | dict[str, Any] | None,
        result: str,
        elapsed_ms: int,
        success: bool,
        image_parts: tuple[dict[str, Any], ...] = (),
        trace_target: TraceTarget | None | object = _CURRENT_TRACE_TARGET,
    ) -> None:
        if self.trace_recorder is None:
            return
        target = (
            self.trace_recorder.capture_target()
            if trace_target is _CURRENT_TRACE_TARGET
            else trace_target
        )
        self.trace_recorder.record_for(
            target if isinstance(target, tuple) else None,
            "tool_result",
            tool_call_id=tool_call_id,
            tool=name,
            arguments=arguments,
            result=result,
            elapsed_ms=elapsed_ms,
            success=success,
            image_parts=image_parts,
        )

    @staticmethod
    def _parse_arguments(arguments: str | dict[str, Any] | None) -> dict[str, Any]:
        if arguments is None or arguments == "":
            return {}
        if isinstance(arguments, dict):
            return arguments
        try:
            parsed = json.loads(arguments)
        except json.JSONDecodeError as exc:
            raise ToolExecutionError(f"Invalid tool arguments JSON: {arguments}") from exc
        if not isinstance(parsed, dict):
            raise ToolExecutionError("Tool arguments must decode to a JSON object.")
        return parsed


def _sanitize_file_mutation_arguments(
    name: str,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    if name == "write_file" and isinstance(arguments.get("content"), str):
        content = str(arguments["content"])
        arguments["content"] = f"[full content omitted: {len(content)} characters]"
    if name == "apply_patch" and isinstance(arguments.get("edits"), list):
        edits = arguments["edits"]
        old_chars = 0
        new_chars = 0
        replace_all_count = 0
        for edit in edits:
            if not isinstance(edit, dict):
                continue
            old_text = edit.get("old_text")
            new_text = edit.get("new_text")
            old_chars += len(old_text) if isinstance(old_text, str) else 0
            new_chars += len(new_text) if isinstance(new_text, str) else 0
            replace_all_count += edit.get("replace_all") is True
        arguments["edits"] = {
            "edit_count": len(edits),
            "old_chars": old_chars,
            "new_chars": new_chars,
            "replace_all_count": replace_all_count,
            "content": "[exact patch text omitted]",
        }
    return arguments
