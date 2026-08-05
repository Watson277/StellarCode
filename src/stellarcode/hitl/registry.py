from __future__ import annotations

import json
from typing import Any

from stellarcode.hitl.handler import HitlHandler
from stellarcode.hitl.model import ApprovalRequest
from stellarcode.hitl.policy import ApprovalPolicy
from stellarcode.command_policy import (
    is_full_disk_recursive_scan,
    restricted_scan_message,
)
from stellarcode.tools.registry import (
    ToolDefinition,
    ToolExecutionResult,
    ToolInvocation,
    ToolRegistry,
)


class HitlToolRegistry(ToolRegistry):
    """Intercepts dangerous tools while preserving the ToolRegistry interface."""

    def __init__(self, delegate: ToolRegistry, hitl_handler: HitlHandler) -> None:
        super().__init__(
            max_parallel_tools=delegate.max_parallel_tools,
            batch_timeout_seconds=delegate.batch_timeout_seconds,
            trace_recorder=delegate.trace_recorder,
        )
        self.delegate = delegate
        self.hitl_handler = hitl_handler

    def register(self, tool: ToolDefinition) -> None:
        self.delegate.register(tool)

    def unregister(self, name: str) -> None:
        self.delegate.unregister(name)

    def list_tools(self) -> list[ToolDefinition]:
        return self.delegate.list_tools()

    def schemas(self) -> list[dict[str, Any]]:
        return self.delegate.schemas()

    def execute(self, name: str, arguments: str | dict[str, Any] | None) -> str:
        if not self.hitl_handler.is_enabled():
            return self.delegate.execute(name, arguments)
        policy_result = _restricted_policy_result(name, arguments)
        if policy_result is not None:
            self._record_intercepted(name, arguments, policy_result)
            return policy_result
        if not ApprovalPolicy.requires_approval(name, arguments):
            return self.delegate.execute(name, arguments)

        original_arguments = _serialize_arguments(arguments)
        request = ApprovalRequest.create(name, original_arguments)
        result = self.hitl_handler.request_approval(request)

        if result.is_rejected:
            reason = result.reason or "The user rejected this operation."
            message = f"[HITL] Operation rejected: {reason}"
            self._record_intercepted(name, arguments, message)
            return message
        if result.is_skipped:
            message = "[HITL] Operation skipped by the user."
            self._record_intercepted(name, arguments, message)
            return message
        return self.delegate.execute(
            name,
            result.effective_arguments(original_arguments),
        )

    def execute_tools(
        self,
        invocations: list[ToolInvocation],
        timeout_seconds: float | None = None,
    ) -> list[ToolExecutionResult]:
        if not self.hitl_handler.is_enabled():
            return self.delegate.execute_tools(invocations, timeout_seconds)

        prepared: list[tuple[int, ToolInvocation]] = []
        immediate: dict[int, ToolExecutionResult] = {}
        for index, invocation in enumerate(invocations):
            policy_result = _restricted_policy_result(
                invocation.name,
                invocation.arguments,
            )
            if policy_result is not None:
                result = _approval_result(invocation, policy_result)
                immediate[index] = result
                self._record_intercepted(invocation.name, invocation.arguments, result.result)
                continue
            if not ApprovalPolicy.requires_approval(
                invocation.name,
                invocation.arguments,
            ):
                prepared.append((index, invocation))
                continue

            original_arguments = _serialize_arguments(invocation.arguments)
            approval = self.hitl_handler.request_approval(
                ApprovalRequest.create(invocation.name, original_arguments)
            )
            if approval.is_rejected:
                reason = approval.reason or "The user rejected this operation."
                immediate[index] = _approval_result(
                    invocation,
                    f"[HITL] Operation rejected: {reason}",
                )
                self._record_intercepted(
                    invocation.name,
                    invocation.arguments,
                    immediate[index].result,
                )
                continue
            if approval.is_skipped:
                immediate[index] = _approval_result(
                    invocation,
                    "[HITL] Operation skipped by the user.",
                )
                self._record_intercepted(
                    invocation.name,
                    invocation.arguments,
                    immediate[index].result,
                )
                continue
            prepared.append(
                (
                    index,
                    ToolInvocation(
                        id=invocation.id,
                        name=invocation.name,
                        arguments=approval.effective_arguments(original_arguments),
                    ),
                )
            )

        executed = self.delegate.execute_tools(
            [invocation for _, invocation in prepared],
            timeout_seconds,
        )
        for (index, _), result in zip(prepared, executed):
            immediate[index] = result
        return [immediate[index] for index in range(len(invocations))]

    def _record_intercepted(
        self,
        name: str,
        arguments: str | dict[str, Any] | None,
        result: str,
    ) -> None:
        if self.trace_recorder is None:
            return
        self.trace_recorder.record(
            "tool_result",
            tool_call_id="intercepted",
            tool=name,
            arguments=arguments,
            result=result,
            elapsed_ms=0,
            success=False,
        )


def _serialize_arguments(arguments: str | dict[str, Any] | None) -> str:
    if arguments is None or arguments == "":
        return "{}"
    if isinstance(arguments, str):
        return arguments
    return json.dumps(arguments, ensure_ascii=False, separators=(",", ":"))


def _approval_result(
    invocation: ToolInvocation,
    result: str,
) -> ToolExecutionResult:
    return ToolExecutionResult(
        id=invocation.id,
        name=invocation.name,
        arguments=invocation.arguments,
        result=result,
        elapsed_ms=0,
        success=False,
    )


def _restricted_policy_result(
    name: str,
    arguments: str | dict[str, Any] | None,
) -> str | None:
    if name != "execute_command":
        return None
    try:
        parsed = json.loads(arguments) if isinstance(arguments, str) else arguments
    except json.JSONDecodeError:
        return None
    if not isinstance(parsed, dict):
        return None
    command = parsed.get("command")
    if isinstance(command, str) or (
        isinstance(command, list) and all(isinstance(part, str) for part in command)
    ):
        if is_full_disk_recursive_scan(command):
            return restricted_scan_message()
    return None
