"""Thread-safe JSON-RPC request/response correlation for MCP transports."""

from __future__ import annotations

import threading
import time
from queue import Empty, Full, Queue
from dataclasses import dataclass, field
from typing import Any, Callable

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
    _NOTIFICATION_QUEUE_LIMIT = 256

    def __init__(self, transport: McpTransport) -> None:
        self.transport = transport
        self._next_id = 1
        self._lock = threading.Lock()
        self._pending: dict[int, _PendingRequest] = {}
        self._notification_listeners: list[Callable[[dict[str, Any]], None]] = []
        self._notification_queue: Queue[dict[str, Any] | None] = Queue(
            maxsize=self._NOTIFICATION_QUEUE_LIMIT
        )
        self._closed = False
        self.transport.on_receive(self._handle_message)
        self._notification_thread = threading.Thread(
            target=self._dispatch_notifications,
            name="stellarcode-mcp-notifications",
            daemon=True,
        )
        self._notification_thread.start()

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
            self.transport.send(message) # Send the JSON-RPC request message through the transport
        except Exception:
            with self._lock:
                self._pending.pop(request_id, None)
            raise
        deadline = time.monotonic() + timeout_seconds
        while not pending.event.wait(min(0.1, max(0.0, deadline - time.monotonic()))):
            exit_code = self.transport.process_exit_code
            if exit_code is not None:  # Process has exited
                with self._lock:
                    self._pending.pop(request_id, None)
                stderr = "\n".join(self.transport.stderr_lines()[-8:]).strip()
                detail = f": {stderr}" if stderr else ""
                raise McpTransportError(
                    f"MCP stdio server exited with code {exit_code} during {method}{detail}"
                )
            if time.monotonic() >= deadline: # Timeout
                with self._lock:
                    self._pending.pop(request_id, None)
                raise TimeoutError(f"JSON-RPC request timed out: {method}")
        if pending.error is not None: # Error occurred
            raise pending.error
        return pending.result # Return the result of the request

    def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        message: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            message["params"] = params
        self.transport.send(message)

    def on_notification(self, listener: Callable[[dict[str, Any]], None]) -> None:
        with self._lock:
            if self._closed:
                raise RuntimeError("JSON-RPC client is closed.")
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
        while True:
            try:
                self._notification_queue.get_nowait()
            except Empty:
                break
        self._notification_queue.put_nowait(None)
        if threading.current_thread() is not self._notification_thread:
            self._notification_thread.join(timeout=1)

    def _handle_message(self, message: dict[str, Any]) -> None:
        request_id = message.get("id")
        if request_id is None:
            # Never execute user/manager callbacks on a transport reader thread.
            # A callback may issue another JSON-RPC request whose response must be
            # consumed by that same reader; synchronous dispatch would deadlock.
            with self._lock:
                closed = self._closed
            if not closed:
                self._enqueue_notification(message)
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

    def _enqueue_notification(self, message: dict[str, Any] | None) -> None:
        """Queue without blocking a transport reader; retain the newest events."""

        try:
            self._notification_queue.put_nowait(message)
            return
        except Full:
            pass
        try:
            self._notification_queue.get_nowait()
        except Empty:
            pass
        try:
            self._notification_queue.put_nowait(message)
        except Full:
            # Another producer won the slot. Dropping is safer than blocking the
            # transport thread and preventing pending responses from being read.
            pass

    def _dispatch_notifications(self) -> None:
        while True:
            message = self._notification_queue.get()
            if message is None:
                return
            with self._lock:
                listeners = list(self._notification_listeners)
            for listener in listeners:
                try:
                    listener(message)
                except Exception:
                    # One third-party notification must not terminate dispatch for
                    # this MCP connection or prevent other listeners from running.
                    continue
