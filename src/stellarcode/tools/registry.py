from __future__ import annotations

import contextvars
import json
import queue
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from stellarcode.trace import TraceRecorder


class ToolExecutionError(RuntimeError):
    """Raised when a tool cannot be executed."""


@dataclass(frozen=True)
class ToolDefinition:
    name: str
    description: str
    parameters: dict[str, Any]
    handler: Callable[..., str | ToolOutput]

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

    def execute(self, name: str, arguments: str | dict[str, Any] | None) -> str:
        started_at = time.monotonic()
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
        try:
            result = tool.handler(**kwargs)
            if isinstance(result, ToolOutput):
                return result
            return ToolOutput(str(result))
        except ToolExecutionError:
            raise
        except Exception as exc:
            raise ToolExecutionError(f"{name} failed: {exc}") from exc

    def execute_tools(
        self,
        invocations: list[ToolInvocation],
        timeout_seconds: float | None = None,
    ) -> list[ToolExecutionResult]:
        if not invocations:
            return []
        if len(invocations) == 1:
            return [self._execute_invocation(invocations[0])]

        batch_timeout = (
            self.batch_timeout_seconds if timeout_seconds is None else timeout_seconds
        )
        if batch_timeout <= 0:
            raise ValueError("timeout_seconds must be greater than 0.")

        work_queue: queue.Queue[tuple[int, ToolInvocation]] = queue.Queue()
        for index, invocation in enumerate(invocations):
            work_queue.put((index, invocation))

        results: list[ToolExecutionResult | None] = [None] * len(invocations)
        deadline = time.monotonic() + batch_timeout

        def run_worker() -> None:
            while time.monotonic() < deadline:
                try:
                    index, invocation = work_queue.get_nowait()
                except queue.Empty:
                    return
                results[index] = self._execute_invocation(invocation)
                work_queue.task_done()

        threads = []
        for index in range(min(len(invocations), self.max_parallel_tools)):
            execution_context = contextvars.copy_context()
            threads.append(
                threading.Thread(
                    target=execution_context.run,
                    args=(run_worker,),
                    name=f"stellarcode-tool-{index + 1}",
                    daemon=True,
                )
            )
        for thread in threads:
            thread.start()
        for thread in threads:
            remaining = max(0.0, deadline - time.monotonic())
            thread.join(remaining)

        timeout_ms = int(batch_timeout * 1000)
        completed_results = []
        for invocation, result in zip(invocations, results):
            if result is not None:
                completed_results.append(result)
                continue
            timeout_result = ToolExecutionResult(
                id=invocation.id,
                name=invocation.name,
                arguments=invocation.arguments,
                result=(
                    f"Tool error: {invocation.name} timed out after "
                    f"{batch_timeout:g}s (batch timeout)."
                ),
                elapsed_ms=timeout_ms,
                success=False,
                timed_out=True,
            )
            self._record_tool_result(
                tool_call_id=invocation.id,
                name=invocation.name,
                arguments=invocation.arguments,
                result=timeout_result.result,
                elapsed_ms=timeout_ms,
                success=False,
            )
            completed_results.append(timeout_result)
        return completed_results

    def _execute_invocation(self, invocation: ToolInvocation) -> ToolExecutionResult:
        started_at = time.monotonic()
        try:
            output = self._execute_output(invocation.name, invocation.arguments)
            result = output.text
            trace_result = output.trace_text if output.trace_text is not None else result
            image_parts = output.image_parts
            success = True
        except ToolExecutionError as exc:
            result = f"Tool error: {exc}"
            trace_result = result
            image_parts = ()
            success = False
        elapsed_ms = int((time.monotonic() - started_at) * 1000)
        self._record_tool_result(
            tool_call_id=invocation.id,
            name=invocation.name,
            arguments=invocation.arguments,
            result=trace_result,
            elapsed_ms=elapsed_ms,
            success=success,
            image_parts=image_parts,
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
    ) -> None:
        if self.trace_recorder is None:
            return
        self.trace_recorder.record(
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
