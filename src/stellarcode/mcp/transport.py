"""Stdio and Streamable HTTP transports; JSON-RPC semantics remain in McpClient."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
from abc import ABC, abstractmethod
from collections import deque
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx


MCP_PROTOCOL_VERSION = "2025-03-26"
MessageReceiver = Callable[[dict[str, Any]], None]


class McpTransportError(RuntimeError):
    """Raised when an MCP transport cannot send or receive a message."""


class McpTransport(ABC):
    @abstractmethod
    def send(self, message: dict[str, Any]) -> None:
        ...

    @abstractmethod
    def on_receive(self, receiver: MessageReceiver) -> None:
        ...

    @property
    @abstractmethod
    def name(self) -> str:
        ...

    @property
    def process_id(self) -> int | None:
        return None

    @property
    def process_exit_code(self) -> int | None:
        return None

    def stderr_lines(self) -> list[str]:
        return []

    @abstractmethod
    def close(self) -> None:
        ...


class StdioTransport(McpTransport):
    def __init__(
        self,
        command: str,
        args: list[str] | None = None,
        env: dict[str, str] | None = None,
        working_dir: str | Path | None = None,
    ) -> None:
        executable = shutil.which(command) or command
        process_env = os.environ.copy()
        process_env.update(env or {})
        try:
            self._process = subprocess.Popen(
                [executable, *(args or [])],
                cwd=Path(working_dir).resolve() if working_dir else None,
                env=process_env,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
            )
        except OSError as exc:
            raise McpTransportError(f"Could not start MCP command {command!r}: {exc}") from exc
        self._receiver: MessageReceiver = lambda _message: None
        self._send_lock = threading.Lock()
        self._stderr: deque[str] = deque(maxlen=200)
        self._stderr_lock = threading.Lock()
        self._closed = False
        self._start_reader_threads()

    @property
    def name(self) -> str:
        return "stdio"

    @property
    def process_id(self) -> int | None:
        return self._process.pid

    @property
    def process_exit_code(self) -> int | None:
        return self._process.poll()

    def on_receive(self, receiver: MessageReceiver) -> None:
        self._receiver = receiver

    def send(self, message: dict[str, Any]) -> None:
        if self._closed or self._process.stdin is None:
            raise McpTransportError("MCP stdio transport is closed.")
        payload = json.dumps(message, ensure_ascii=False, separators=(",", ":"))
        try:
            with self._send_lock:
                self._process.stdin.write(payload + "\n")
                self._process.stdin.flush()
        except (OSError, ValueError) as exc:
            raise McpTransportError(f"Could not write to MCP stdio server: {exc}") from exc

    def stderr_lines(self) -> list[str]:
        with self._stderr_lock:
            return list(self._stderr)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._process.stdin is not None:
            try:
                self._process.stdin.close()
            except OSError:
                pass
        try:
            self._process.wait(timeout=1)
            return
        except subprocess.TimeoutExpired:
            self._process.terminate()
        try:
            self._process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            self._process.kill()

    def _start_reader_threads(self) -> None:
        threading.Thread(
            target=self._read_stdout,
            name="stellarcode-mcp-stdio-stdout",
            daemon=True,
        ).start()
        threading.Thread(
            target=self._read_stderr,
            name="stellarcode-mcp-stdio-stderr",
            daemon=True,
        ).start()

    def _read_stdout(self) -> None:
        if self._process.stdout is None:
            return
        try:
            for line in self._process.stdout:
                if not line.strip():
                    continue
                try:
                    message = json.loads(line)
                    if isinstance(message, dict):
                        self._receiver(message)
                except json.JSONDecodeError as exc:
                    self._append_stderr(f"[stellarcode] invalid JSON on stdout: {exc}")
        except (OSError, ValueError) as exc:
            if not self._closed:
                self._append_stderr(f"[stellarcode] stdout reader stopped: {exc}")

    def _read_stderr(self) -> None:
        if self._process.stderr is None:
            return
        try:
            for line in self._process.stderr:
                self._append_stderr(line.rstrip("\r\n"))
        except (OSError, ValueError) as exc:
            if not self._closed:
                self._append_stderr(f"[stellarcode] stderr reader stopped: {exc}")

    def _append_stderr(self, line: str) -> None:
        with self._stderr_lock:
            self._stderr.append(line)


class StreamableHttpTransport(McpTransport):
    def __init__(
        self,
        url: str,
        headers: dict[str, str] | None = None,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.url = url
        self.headers = dict(headers or {})
        self._receiver: MessageReceiver = lambda _message: None
        self._session_id = ""
        self._session_lock = threading.Lock()
        self._client = httpx.Client(timeout=60, transport=transport)
        self._closed = False

    @property
    def name(self) -> str:
        return "http"

    def on_receive(self, receiver: MessageReceiver) -> None:
        self._receiver = receiver

    def send(self, message: dict[str, Any]) -> None:
        if self._closed:
            raise McpTransportError("MCP HTTP transport is closed.")
        request_headers = self._request_headers()
        try:
            response = self._client.post(self.url, headers=request_headers, json=message)
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise McpTransportError(f"MCP HTTP request failed: {exc}") from exc
        session_id = response.headers.get("Mcp-Session-Id", "").strip()
        if session_id:
            with self._session_lock:
                self._session_id = session_id
        if not response.content or not response.text.strip():
            return
        content_type = response.headers.get("content-type", "").lower()
        try:
            messages = (
                _parse_sse(response.text)
                if "text/event-stream" in content_type
                else _json_messages(response.json())
            )
        except (json.JSONDecodeError, ValueError) as exc:
            raise McpTransportError(f"Invalid MCP HTTP response: {exc}") from exc
        for item in messages:
            self._receiver(item)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        with self._session_lock:
            session_id = self._session_id
        if session_id:
            try:
                self._client.delete(
                    self.url,
                    headers={
                        **self.headers,
                        "MCP-Protocol-Version": MCP_PROTOCOL_VERSION,
                        "Mcp-Session-Id": session_id,
                    },
                    timeout=5,
                )
            except httpx.HTTPError:
                pass
        self._client.close()

    def _request_headers(self) -> dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "MCP-Protocol-Version": MCP_PROTOCOL_VERSION,
            **self.headers,
        }
        with self._session_lock:
            if self._session_id:
                headers["Mcp-Session-Id"] = self._session_id
        return headers


def _parse_sse(raw: str) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = []
    data_lines: list[str] = []
    for line in raw.splitlines() + [""]:
        if not line:
            if data_lines:
                messages.extend(_json_messages(json.loads("\n".join(data_lines))))
                data_lines.clear()
            continue
        if line.startswith("data:"):
            data_lines.append(line.removeprefix("data:").strip())
    return messages


def _json_messages(value: object) -> list[dict[str, Any]]:
    if isinstance(value, dict):
        return [value]
    if isinstance(value, list) and all(isinstance(item, dict) for item in value):
        return list(value)
    raise ValueError("MCP response must be a JSON object or object array.")
