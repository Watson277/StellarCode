"""Approval-aware ToolRegistry wrapper; policy approval never bypasses tool guards."""

from __future__ import annotations

import json
import threading
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
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
    """Approval decorator placed in front of the real ``ToolRegistry``.

    ``delegate`` remains the single owner of tool definitions, schemas, execution
    workers, timeouts, and concrete handlers.  This wrapper only adds the HITL
    decision boundary before forwarding an approved invocation to that registry.
    Keeping this distinction explicit prevents approval/UI concerns from leaking
    into the source ``ToolRegistry`` implementation.
    """

    def __init__(self, delegate: ToolRegistry, hitl_handler: HitlHandler) -> None:
        super().__init__(
            max_parallel_tools=delegate.max_parallel_tools,
            batch_timeout_seconds=delegate.batch_timeout_seconds,
            trace_recorder=delegate.trace_recorder,
        )
        # The inherited registry state exists only for ToolRegistry-compatible typing.
        # All real registrations and executions are owned by this delegate.
        self.delegate = delegate
        # The handler supplies the decision channel: terminal input or Runtime events.
        self.hitl_handler = hitl_handler

    # -------------------------------------------------------------------------
    # ToolRegistry compatibility forwarding
    # -------------------------------------------------------------------------
    # These methods deliberately do not use the inherited ToolRegistry storage.
    # Dynamic built-in/MCP tools must have one authoritative registry: delegate.
    def register(self, tool: ToolDefinition) -> None:
        self.delegate.register(tool)

    def unregister(self, name: str) -> None:
        self.delegate.unregister(name)

    def replace_tools(
        self,
        remove_names: list[str],
        tools: list[ToolDefinition],
    ) -> None:
        self.delegate.replace_tools(remove_names, tools)

    def list_tools(self) -> list[ToolDefinition]:
        return self.delegate.list_tools()

    def schemas(self) -> list[dict[str, Any]]:
        return self.delegate.schemas()

    def preview(
        self,
        name: str,
        arguments: str | dict[str, Any] | None,
    ) -> dict[str, Any] | None:
        return self.delegate.preview(name, arguments)

    def event_arguments(
        self,
        name: str,
        arguments: str | dict[str, Any] | None,
    ) -> dict[str, Any]:
        return self.delegate.event_arguments(name, arguments)

    def wait_for_quiescence(
        self,
        timeout_seconds: float | None = None,
        *,
        task_id: str | None = None,
    ) -> bool:
        return self.delegate.wait_for_quiescence(
            timeout_seconds,
            task_id=task_id,
        )

    def execute(self, name: str, arguments: str | dict[str, Any] | None) -> str:
        # Full-access mode disables only the approval boundary.  The original
        # ToolRegistry and its concrete handler validation still execute normally.
        if not self.hitl_handler.is_enabled():
            return self.delegate.execute(name, arguments)

        # Non-approvable restricted-mode policy failures are final.  They are not
        # presented as approvals because user consent must not override policy.
        policy_result = _restricted_policy_result(name, arguments)
        if policy_result is not None:
            self._record_intercepted(name, arguments, policy_result)
            return policy_result

        # Safe/read-only tools bypass HITL and retain the source registry's behavior.
        if not ApprovalPolicy.requires_approval(name, arguments):
            return self.delegate.execute(name, arguments)

        # HITL starts here.  Build a read-only preview before asking for a decision;
        # no tool handler with side effects has run at this point.
        original_arguments = _serialize_arguments(arguments)
        change_preview = self.delegate.preview(name, arguments)
        request = ApprovalRequest.create(
            name,
            original_arguments,
            change_preview=change_preview,
            display_arguments=self.delegate.event_arguments(name, arguments),
        )
        result = self.hitl_handler.request_approval(request)

        # A rejection/skip is converted to a normal tool result so the Agent can
        # observe the decision and re-plan instead of treating it as a Runtime crash.
        if result.is_rejected:
            reason = result.reason or "The user rejected this operation."
            message = f"[HITL] Operation rejected: {reason}"
            self._record_intercepted(name, arguments, message)
            return message
        if result.is_skipped:
            message = "[HITL] Operation skipped by the user."
            self._record_intercepted(name, arguments, message)
            return message

        # Modified approval arguments replace the model-proposed arguments.  File
        # changes receive a hidden path/hash guard that is verified by the original
        # handler immediately before mutation (approval-to-write TOCTOU protection).
        effective_arguments = result.effective_arguments(original_arguments)
        effective_preview = (
            self.delegate.preview(name, effective_arguments)
            if result.modified_arguments is not None
            else change_preview
        )
        return self.delegate.execute(
            name,
            _guard_approved_change(
                name,
                effective_arguments,
                effective_preview,
            ),
        )

    def execute_tools(
        self,
        invocations: list[ToolInvocation],
        timeout_seconds: float | None = None,
        cancellation_event: threading.Event | None = None,
    ) -> list[ToolExecutionResult]:
        # Batch execution follows the same decorator rule as execute(): when approval
        # is disabled, delegate owns the complete operation without extra HITL work.
        if not self.hitl_handler.is_enabled():
            return self.delegate.execute_tools(
                invocations,
                timeout_seconds,
                cancellation_event,
            )

        # Phase 1: classify every invocation without executing side effects.
        # ``prepared`` contains safe tools now and approved tools later; ``immediate``
        # contains policy/rejection results; approval candidates wait for the user.
        prepared: list[tuple[int, ToolInvocation]] = []
        immediate: dict[int, ToolExecutionResult] = {}
        approval_candidates: list[
            tuple[int, ToolInvocation, str, dict[str, Any] | None, ApprovalRequest]
        ] = []
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
            change_preview = self.delegate.preview(
                invocation.name,
                invocation.arguments,
            )
            approval_candidates.append(
                (
                    index,
                    invocation,
                    original_arguments,
                    change_preview,
                    ApprovalRequest.create(
                        invocation.name,
                        original_arguments,
                        tool_call_id=invocation.id,
                        change_preview=change_preview,
                        display_arguments=self.delegate.event_arguments(
                            invocation.name,
                            invocation.arguments,
                        ),
                    ),
                )
            )

        approvals: dict[int, Any] = {}
        if approval_candidates:
            # Phase 2: publish the complete approval batch before waiting. Desktop
            # decisions are independent, while TerminalHitlHandler serializes stdin
            # internally so concurrent workers cannot consume each other's answers.
            with ThreadPoolExecutor(
                max_workers=len(approval_candidates),
                thread_name_prefix="stellarcode-approval",
            ) as executor:
                futures = {
                    index: executor.submit(
                        copy_context().run,
                        self.hitl_handler.request_approval,
                        request,
                    )
                    for index, _invocation, _arguments, _preview, request
                    in approval_candidates
                }
                for index, *_rest in approval_candidates:
                    approvals[index] = futures[index].result()

        # Phase 3: turn decisions into either immediate rejection results or guarded
        # invocations ready for the original ToolRegistry.
        for index, invocation, original_arguments, change_preview, _request in approval_candidates:
            approval = approvals[index]
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
            effective_arguments = approval.effective_arguments(original_arguments)
            effective_preview = (
                self.delegate.preview(invocation.name, effective_arguments)
                if approval.modified_arguments is not None
                else change_preview
            )
            prepared.append(
                (
                    index,
                    ToolInvocation(
                        id=invocation.id,
                        name=invocation.name,
                        arguments=_guard_approved_change(
                            invocation.name,
                            effective_arguments,
                            effective_preview,
                        ),
                    ),
                )
            )

        # Phase 4: only the source ToolRegistry performs actual tool execution,
        # including its existing parallelism, timeout, cancellation, and tracing.
        executed = self.delegate.execute_tools(
            [invocation for _, invocation in prepared],
            timeout_seconds,
            cancellation_event,
        )
        for (index, _), result in zip(prepared, executed):
            immediate[index] = result
        # Phase 5: restore model call order even when approvals and tools completed
        # concurrently.  This keeps tool_call/tool_result correlation deterministic.
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


def _guard_approved_change(
    name: str,
    arguments: str | dict[str, Any] | None,
    preview: dict[str, Any] | None,
) -> str | dict[str, Any] | None:
    """Bind an approved file preview to the exact pre-write file state.

    The hidden values never enter the model schema or UI.  The built-in file
    handler rechecks them while holding its mutation lock, closing the window
    between approval and the actual atomic replace/unlink.
    """

    if name not in {"write_file", "apply_patch", "delete_file"}:
        return arguments
    if preview is None:
        return arguments
    try:
        parsed = json.loads(arguments) if isinstance(arguments, str) else arguments
    except json.JSONDecodeError:
        return arguments
    if not isinstance(parsed, dict):
        return arguments
    guarded = dict(parsed)
    guarded["__expected_path"] = str(preview.get("path") or "")
    before_hash = preview.get("before_sha256")
    guarded["__expected_before_sha256"] = (
        str(before_hash) if before_hash is not None else "__stellarcode_missing__"
    )
    return guarded


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
