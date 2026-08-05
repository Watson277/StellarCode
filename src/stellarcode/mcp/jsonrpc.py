from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any

from stellarcode.mcp.transport import McpTransport, McpTransportError


class JsonRpcError(RuntimeError):
    def __init__(self, code: int, message: str) -> None:
        super().__init__(f"JSON-RPC error {code}: {message}")
        self.code = code
        self.message = message


@dataclass
class _PendingRequest:
    event: threading.Event = field(default_factory=threading.Event)
    result: Any = None
    error: BaseException | None = None


class JsonRpcClient:
    def __init__(self, transport: McpTransport) -> None:
        self.transport = transport
        self._next_id = 1
        self._lock = threading.Lock()
        self._pending: dict[int, _PendingRequest] = {}
        self._notification_listeners: list[callable] = []
        self._closed = False
        self.transport.on_receive(self._handle_message)

    def request(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        timeout_seconds: float = 60,
    ) -> Any:
        with self._lock:
            if self._closed:
                raise RuntimeError("JSON-RPC client is closed.")
            request_id = self._next_id
            self._next_id += 1
            pending = _PendingRequest()
            self._pending[request_id] = pending
        message: dict[str, Any] = {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": method,
        }
        if params is not None:
            message["params"] = params
        try:
            self.transport.send(message)
        except Exception:
            with self._lock:
                self._pending.pop(request_id, None)
            raise
        deadline = time.monotonic() + timeout_seconds
        while not pending.event.wait(min(0.1, max(0.0, deadline - time.monotonic()))):
            exit_code = self.transport.process_exit_code
            if exit_code is not None:
                with self._lock:
                    self._pending.pop(request_id, None)
                stderr = "\n".join(self.transport.stderr_lines()[-8:]).strip()
                detail = f": {stderr}" if stderr else ""
                raise McpTransportError(
                    f"MCP stdio server exited with code {exit_code} during {method}{detail}"
                )
            if time.monotonic() >= deadline:
                with self._lock:
                    self._pending.pop(request_id, None)
                raise TimeoutError(f"JSON-RPC request timed out: {method}")
        if pending.error is not None:
            raise pending.error
        return pending.result

    def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        message: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            message["params"] = params
        self.transport.send(message)

    def on_notification(self, listener: callable) -> None:
        self._notification_listeners.append(listener)

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            pending = list(self._pending.values())
            self._pending.clear()
        for request in pending:
            request.error = RuntimeError("JSON-RPC client closed.")
            request.event.set()
        self.transport.close()

    def _handle_message(self, message: dict[str, Any]) -> None:
        request_id = message.get("id")
        if request_id is None:
            for listener in list(self._notification_listeners):
                listener(message)
            return
        if not isinstance(request_id, int):
            return
        with self._lock:
            pending = self._pending.pop(request_id, None)
        if pending is None:
            return
        error = message.get("error")
        if isinstance(error, dict):
            pending.error = JsonRpcError(
                int(error.get("code", -32603)),
                str(error.get("message") or "Unknown JSON-RPC error"),
            )
        else:
            pending.result = message.get("result")
        pending.event.set()
