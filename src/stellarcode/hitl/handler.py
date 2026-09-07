"""Human-in-the-loop approval interface and blocking decision synchronization."""

from __future__ import annotations

import json
import threading
from collections.abc import Callable
from typing import Protocol

from stellarcode.hitl.model import ApprovalRequest, ApprovalResult
from stellarcode.hitl.policy import ApprovalPolicy
from stellarcode.trace import TraceRecorder


class HitlHandler(Protocol):
    def is_enabled(self) -> bool:
        ...

    def set_enabled(self, enabled: bool) -> None:
        ...

    def set_access_mode(self, access_mode: str) -> None:
        ...

    def clear_approved_all(self) -> None:
        ...

    def request_approval(self, request: ApprovalRequest) -> ApprovalResult:
        ...


class TerminalHitlHandler:
    MAX_ATTEMPTS = 5

    def __init__(
        self,
        enabled: bool = False,
        access_mode: str | None = None,
        input_func: Callable[[str], str] = input,
        output_func: Callable[[str], None] = print,
        render_func: Callable[[ApprovalRequest], None] | None = None,
        trace_recorder: TraceRecorder | None = None,
    ) -> None:
        self._access_mode = ApprovalPolicy.validate_access_mode(
            access_mode or ("restricted" if enabled else "full-access")
        )
        self._input = input_func
        self._output = output_func
        self._render = render_func or (lambda request: self._output(request.to_display_text()))
        self.trace_recorder = trace_recorder
        self._approved_all_tools: set[str] = set()
        self._approved_all_servers: set[str] = set()
        self._lock = threading.RLock()

    def is_enabled(self) -> bool:
        with self._lock:
            return ApprovalPolicy.approval_boundary_enabled(self._access_mode)

    def set_enabled(self, enabled: bool) -> None:
        """Compatibility bridge for callers that still use the old boolean API."""

        self.set_access_mode("restricted" if enabled else "full-access")

    def set_access_mode(self, access_mode: str) -> None:
        mode = ApprovalPolicy.validate_access_mode(access_mode)
        with self._lock:
            self._access_mode = mode

    def clear_approved_all(self) -> None:
        with self._lock:
            self._approved_all_tools.clear()
            self._approved_all_servers.clear()

    def clear_approved_all_for_server(self, server_name: str) -> None:
        with self._lock:
            self._approved_all_servers.discard(server_name)

    @property
    def approved_all_tools(self) -> tuple[str, ...]:
        with self._lock:
            return tuple(sorted(self._approved_all_tools))

    @property
    def approved_all_servers(self) -> tuple[str, ...]:
        with self._lock:
            return tuple(sorted(self._approved_all_servers))

    def request_approval(self, request: ApprovalRequest) -> ApprovalResult:
        # One lock covers the complete prompt so concurrent Workers cannot share stdin.
        with self._lock:
            if self.trace_recorder:
                self.trace_recorder.record(
                    "approval_request",
                    tool=request.tool_name,
                    arguments=request.arguments,
                    danger_level=request.danger_level,
                    risk=request.risk_description,
                )
            if not ApprovalPolicy.requires_user_decision(
                self._access_mode,
                request.danger_level,
            ):
                result = ApprovalResult.approved()
                self._record_decision(request, result, automatic=True)
                return result
            server_name = _mcp_server_name(request.tool_name)
            if request.tool_name in self._approved_all_tools or (
                server_name is not None and server_name in self._approved_all_servers
            ):
                self._output(
                    f"[HITL] 本会话已持续允许 {request.tool_name}，继续执行。"
                )
                result = ApprovalResult.approved()
                self._record_decision(request, result, automatic=True)
                return result

            self._output("")
            self._render(request)
            result = self._prompt_until_decision(request)
            self._record_decision(request, result, automatic=False)
            return result

    def _record_decision(
        self,
        request: ApprovalRequest,
        result: ApprovalResult,
        *,
        automatic: bool,
    ) -> None:
        if self.trace_recorder is None:
            return
        self.trace_recorder.record(
            "approval_decision",
            tool=request.tool_name,
            decision=result.decision.value,
            modified_arguments=result.modified_arguments,
            reason=result.reason,
            automatic=automatic,
        )

    def _prompt_until_decision(self, request: ApprovalRequest) -> ApprovalResult:
        for _ in range(self.MAX_ATTEMPTS):
            try:
                choice = self._input(
                    "请选择 [y/Enter] 批准本次、[a] 本会话全部批准、[n] 拒绝、"
                    "[s] 跳过、[m] 修改参数: "
                ).strip().lower()
            except (EOFError, KeyboardInterrupt):
                return ApprovalResult.rejected("Approval input was interrupted.")

            if choice in {"", "y", "yes"}:
                return ApprovalResult.approved()
            if choice == "a":
                return self._approve_for_session(request.tool_name)
            if choice == "n":
                return self._request_rejection_reason()
            if choice == "s":
                return ApprovalResult.skipped()
            if choice == "m":
                modified = self._request_modified_arguments()
                if modified is not None:
                    return ApprovalResult.modified(modified)
                continue
            self._output("无法识别该选项，请输入 y、a、n、s 或 m。")

        return ApprovalResult.rejected("Too many unrecognized approval responses.")

    def _approve_for_session(self, tool_name: str) -> ApprovalResult:
        server_name = _mcp_server_name(tool_name)
        if server_name is None:
            self._approved_all_tools.add(tool_name)
            return ApprovalResult.approved_all()

        for _ in range(self.MAX_ATTEMPTS):
            try:
                scope = self._input(
                    "本会话批准范围: [t/Enter] 仅当前 tool，[s] 整个 MCP server "
                    "（连续操作时使用）: "
                ).strip().lower()
            except (EOFError, KeyboardInterrupt):
                return ApprovalResult.rejected("Approval input was interrupted.")
            if scope in {"s", "server"}:
                self._approved_all_servers.add(server_name)
                self._output(f"[HITL] 本会话已放行 MCP server: {server_name}")
                return ApprovalResult.approved_all()
            if scope in {"", "t", "tool"}:
                self._approved_all_tools.add(tool_name)
                self._output(f"[HITL] 本会话已放行 MCP tool: {tool_name}")
                return ApprovalResult.approved_all()
            self._output("请输入 s（整个 server）或 t（仅当前 tool）。")
        return ApprovalResult.rejected("Too many unrecognized approval scope responses.")

    def _request_rejection_reason(self) -> ApprovalResult:
        try:
            reason = self._input("拒绝原因（可选）: ").strip()
        except (EOFError, KeyboardInterrupt):
            reason = ""
        return ApprovalResult.rejected(reason or "The user rejected this operation.")

    def _request_modified_arguments(self) -> str | None:
        try:
            raw = self._input("请输入新的 JSON 对象参数: ").strip()
            parsed = json.loads(raw)
        except (EOFError, KeyboardInterrupt, json.JSONDecodeError):
            self._output("JSON 参数无效，操作未执行。")
            return None
        if not isinstance(parsed, dict):
            self._output("参数必须是 JSON 对象，操作未执行。")
            return None
        return json.dumps(parsed, ensure_ascii=False, separators=(",", ":"))


def _mcp_server_name(tool_name: str) -> str | None:
    parts = tool_name.split("__", 2)
    if len(parts) != 3 or parts[0] != "mcp" or not parts[1]:
        return None
    return parts[1]
