from __future__ import annotations

import json
import threading
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from stellarcode.hitl import ApprovalRequest, ApprovalResult
from stellarcode.llm.types import current_llm_scope
from stellarcode.tools.registry import ToolRegistry


@dataclass
class _PendingApproval:
    event: threading.Event = field(default_factory=threading.Event)
    result: ApprovalResult | None = None
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
    ) -> None:
        self._emit = emit
        self._enabled = enabled
        self._pending: dict[str, _PendingApproval] = {}
        self._lock = threading.RLock()

    def is_enabled(self) -> bool:
        with self._lock:
            return self._enabled

    def set_enabled(self, enabled: bool) -> None:
        with self._lock:
            self._enabled = enabled

    def clear_approved_all(self) -> None:
        # Session-wide approval is intentionally not persisted by the v1 desktop protocol.
        return None

    def request_approval(self, request: ApprovalRequest) -> ApprovalResult:
        approval_id = f"approval-{uuid.uuid4().hex}"
        session_id, task_id = current_llm_scope()
        pending = _PendingApproval(
            tool_name=request.tool_name,
            session_id=session_id,
            task_id=task_id,
        )
        with self._lock:
            self._pending[approval_id] = pending
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
                "danger_level": "low" if request.danger_level == "safe" else request.danger_level,
                "risk_description": request.risk_description,
                "change_preview": request.change_preview,
            },
        )
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
            if pending is None:
                return False
            if decision == "approve":
                pending.result = ApprovalResult.approved()
            elif decision == "skip":
                pending.result = ApprovalResult.skipped()
            elif decision == "reject":
                pending.result = ApprovalResult.rejected("The user rejected this operation.")
            elif decision == "modify" and effective_arguments is not None:
                pending.result = ApprovalResult.modified(
                    json.dumps(effective_arguments, ensure_ascii=False, separators=(",", ":"))
                )
            else:
                return False
            if before_release is not None:
                before_release()
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
            if pending is None:
                return None
            return pending.session_id, pending.task_id

    def reject_task(self, task_id: str, reason: str) -> None:
        with self._lock:
            for pending in self._pending.values():
                if pending.task_id != task_id:
                    continue
                pending.result = ApprovalResult.rejected(reason)
                pending.event.set()

    def reject_all(self, reason: str) -> None:
        with self._lock:
            pending = list(self._pending.values())
            for item in pending:
                item.result = ApprovalResult.rejected(reason)
                item.event.set()


def _parse_arguments(arguments: str) -> dict[str, Any]:
    try:
        value = json.loads(arguments)
    except json.JSONDecodeError:
        return {"value": arguments}
    return value if isinstance(value, dict) else {"value": value}
