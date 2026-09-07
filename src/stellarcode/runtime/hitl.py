"""Runtime-backed HITL queue that correlates approval decisions with task/session IDs."""

from __future__ import annotations

import json
import threading
import uuid
from collections.abc import Callable
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any, Iterator

from stellarcode.hitl import ApprovalPolicy, ApprovalRequest, ApprovalResult
from stellarcode.llm.types import current_llm_scope
from stellarcode.tools.registry import ToolRegistry


_TASK_ACCESS_MODE: ContextVar[str | None] = ContextVar(
    "stellarcode_task_access_mode",
    default=None,
)


@contextmanager
def task_approval_scope(access_mode: str) -> Iterator[None]:
    """Freeze one task's approval policy across copied worker contexts."""

    mode = ApprovalPolicy.validate_access_mode(access_mode)
    token = _TASK_ACCESS_MODE.set(mode)
    try:
        yield
    finally:
        _TASK_ACCESS_MODE.reset(token)


@dataclass
class _PendingApproval:
    event: threading.Event = field(default_factory=threading.Event)
    result: ApprovalResult | None = None
    # Decisions are single-assignment.  In particular, cancellation rejects an
    # approval before waking the blocked tool thread; the short interval before
    # that thread removes the entry must not allow a late desktop approval to
    # overwrite the rejection.
    settled: bool = False
    tool_name: str = ""
    session_id: str = ""
    task_id: str = ""


class RuntimeHitlHandler:
    """Bridges blocking tool approval calls to asynchronous RuntimeEvent messages."""

    def __init__(
        self,
        emit: Callable[[str, dict[str, Any]], None],
        *,
        enabled: bool = True,
        access_mode: str | None = None,
        is_task_cancelled: Callable[[str], bool] | None = None,
    ) -> None:
        self._emit = emit
        self._access_mode = ApprovalPolicy.validate_access_mode(
            access_mode or ("restricted" if enabled else "full-access")
        )
        self._is_task_cancelled = is_task_cancelled or (lambda _task_id: False)
        self._pending: dict[str, _PendingApproval] = {}
        self._lock = threading.RLock()

    def is_enabled(self) -> bool:
        return ApprovalPolicy.approval_boundary_enabled(self._current_access_mode())

    def _current_access_mode(self) -> str:
        task_override = _TASK_ACCESS_MODE.get()
        if task_override is not None:
            return task_override
        with self._lock:
            return self._access_mode

    def set_enabled(self, enabled: bool) -> None:
        """Compatibility bridge for callers that still use the old boolean API."""

        self.set_access_mode("restricted" if enabled else "full-access")

    def set_access_mode(self, access_mode: str) -> None:
        mode = ApprovalPolicy.validate_access_mode(access_mode)
        with self._lock:
            self._access_mode = mode

    def clear_approved_all(self) -> None:
        # Session-wide approval is intentionally not persisted by the v1 desktop protocol.
        return None

    def request_approval(self, request: ApprovalRequest) -> ApprovalResult:
        approval_id = f"approval-{uuid.uuid4().hex}"
        session_id, task_id = current_llm_scope()
        if task_id and self._is_task_cancelled(task_id):
            return ApprovalResult.rejected("Task cancelled by user.")
        if not ApprovalPolicy.requires_user_decision(
            self._current_access_mode(),
            request.danger_level,
        ):
            return ApprovalResult.approved()
        pending = _PendingApproval(
            tool_name=request.tool_name,
            session_id=session_id,
            task_id=task_id,
        )
        with self._lock:
            self._pending[approval_id] = pending
            # Close the check/add race with cancel_task(): if cancellation
            # happened between the first predicate read and registration,
            # reject locally. Holding the HITL lock through emit also guarantees
            # cancel sees either no request or one already visible to the UI.
            if task_id and self._is_task_cancelled(task_id):
                self._pending.pop(approval_id, None)
                return ApprovalResult.rejected("Task cancelled by user.")
            try:
                self._emit(
                    "approval.requested",
                    {
                        "approval_id": approval_id,
                        "tool_call_id": request.tool_call_id or approval_id,
                        "name": request.tool_name,
                        "arguments": (
                            request.display_arguments
                            if request.display_arguments is not None
                            else _parse_arguments(request.arguments)
                        ),
                        "danger_level": (
                            "low" if request.danger_level == "safe" else request.danger_level
                        ),
                        "risk_description": request.risk_description,
                        "change_preview": request.change_preview,
                    },
                )
            except BaseException:
                self._pending.pop(approval_id, None)
                raise
        pending.event.wait()
        with self._lock:
            self._pending.pop(approval_id, None)
        return pending.result or ApprovalResult.rejected("Runtime approval was interrupted.")

    def resolve(
        self,
        approval_id: str,
        decision: str,
        effective_arguments: dict[str, Any] | None = None,
        *,
        before_release: Callable[[], None] | None = None,
    ) -> bool:
        with self._lock:
            pending = self._pending.get(approval_id)
            if pending is None or pending.settled:
                return False
            if decision == "approve":
                result = ApprovalResult.approved()
            elif decision == "skip":
                result = ApprovalResult.skipped()
            elif decision == "reject":
                result = ApprovalResult.rejected("The user rejected this operation.")
            elif decision == "modify" and effective_arguments is not None:
                result = ApprovalResult.modified(
                    json.dumps(effective_arguments, ensure_ascii=False, separators=(",", ":"))
                )
            else:
                return False
            # The durable approval.resolved event is the commit point.  Do not
            # settle or wake the tool when journaling it fails; a later retry can
            # then make a fresh, auditable decision.
            if before_release is not None:
                before_release()
            pending.result = result
            pending.settled = True
            pending.event.set()
            return True

    def safe_effective_arguments(
        self,
        approval_id: str,
        effective_arguments: dict[str, Any] | None,
    ) -> dict[str, Any] | None:
        if effective_arguments is None:
            return None
        with self._lock:
            pending = self._pending.get(approval_id)
            tool_name = pending.tool_name if pending is not None else ""
        return ToolRegistry.sanitize_event_arguments(tool_name, effective_arguments)

    def context(self, approval_id: str) -> tuple[str, str] | None:
        with self._lock:
            pending = self._pending.get(approval_id)
            if pending is None or pending.settled:
                return None
            return pending.session_id, pending.task_id

    def reject_task(self, task_id: str, reason: str) -> None:
        with self._lock:
            for pending in self._pending.values():
                if pending.task_id != task_id or pending.settled:
                    continue
                pending.result = ApprovalResult.rejected(reason)
                pending.settled = True
                pending.event.set()

    def reject_all(self, reason: str) -> None:
        with self._lock:
            pending = list(self._pending.values())
            for item in pending:
                if item.settled:
                    continue
                item.result = ApprovalResult.rejected(reason)
                item.settled = True
                item.event.set()


def _parse_arguments(arguments: str) -> dict[str, Any]:
    try:
        value = json.loads(arguments)
    except json.JSONDecodeError:
        return {"value": arguments}
    return value if isinstance(value, dict) else {"value": value}
