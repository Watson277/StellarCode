from __future__ import annotations

import json
import re
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


_SENSITIVE_KEY = re.compile(
    r"(?:api[_-]?key|authorization|cookie|password|secret|token)",
    re.IGNORECASE,
)
_BEARER = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+")
_INLINE_SECRET = re.compile(
    r"(?i)\b(api[_-]?key|authorization|password|secret|token)"
    r"(\s*[:=]\s*)([^\s,;]+)"
)
_DATA_IMAGE = re.compile(r"data:image/([^;,]+);base64,([A-Za-z0-9+/=]+)")


class TraceRecorder:
    """Thread-safe, opt-in JSONL recorder for complete Agent execution traces."""

    def __init__(self, directory: str | Path) -> None:
        self.directory = Path(directory)
        self._enabled = False
        self._path: Path | None = None
        self._session_id = ""
        self._lock = threading.RLock()
        self._last_error = ""

    @property
    def enabled(self) -> bool:
        with self._lock:
            return self._enabled

    @property
    def path(self) -> Path | None:
        with self._lock:
            return self._path

    @property
    def last_error(self) -> str:
        with self._lock:
            return self._last_error

    def enable(self, **metadata: object) -> Path:
        with self._lock:
            if self._enabled and self._path is not None:
                return self._path
            now = datetime.now()
            self._session_id = uuid.uuid4().hex
            self._path = self.directory / (
                f"session-{now:%Y%m%d-%H%M%S}-{self._session_id[:8]}.jsonl"
            )
            self._enabled = True
            self._last_error = ""
            self._write(
                "session_start",
                {
                    "local_time": now.astimezone().isoformat(),
                    **metadata,
                },
            )
            return self._path

    def disable(self) -> Path | None:
        with self._lock:
            path = self._path
            if self._enabled:
                self._write("session_stop", {})
            self._enabled = False
            return path

    def close(self) -> None:
        self.disable()

    def record(self, event: str, **data: object) -> None:
        with self._lock:
            if not self._enabled:
                return
            self._write(event, data)

    def status(self) -> str:
        with self._lock:
            state = "on" if self._enabled else "off"
            path = str(self._path) if self._path else "(no trace file yet)"
            message = f"trace mode: {state}; file: {path}"
            if self._last_error:
                message += f"; last error: {self._last_error}"
            return message

    def _write(self, event: str, data: dict[str, object]) -> None:
        if self._path is None:
            return
        entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "session_id": self._session_id,
            "thread": threading.current_thread().name,
            "event": event,
            "data": _sanitize(data),
        }
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            line = json.dumps(entry, ensure_ascii=False, default=_json_default)
            with self._path.open("a", encoding="utf-8") as stream:
                stream.write(line + "\n")
                stream.flush()
        except Exception as exc:
            self._last_error = f"{type(exc).__name__}: {exc}"


class TracingChatClient:
    """Chat client decorator that records complete requests, responses, and errors."""

    def __init__(self, delegate: object, recorder: TraceRecorder) -> None:
        self.delegate = delegate
        self.recorder = recorder

    def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        temperature: float = 0.2,
    ) -> dict[str, Any]:
        model = _request_model(self.delegate, messages)
        self.recorder.record(
            "llm_request",
            model=model,
            temperature=temperature,
            messages=messages,
            tools=tools or [],
        )
        started_at = time.monotonic()
        try:
            response = self.delegate.chat(messages, tools=tools, temperature=temperature)
        except Exception as exc:
            self.recorder.record(
                "llm_error",
                model=model,
                elapsed_ms=int((time.monotonic() - started_at) * 1000),
                error_type=type(exc).__name__,
                error=str(exc),
            )
            raise
        self.recorder.record(
            "llm_response",
            model=model,
            elapsed_ms=int((time.monotonic() - started_at) * 1000),
            response=response,
        )
        return response

    def __getattr__(self, name: str) -> object:
        return getattr(self.delegate, name)


def _request_model(client: object, messages: list[dict[str, Any]]) -> str:
    selector = getattr(client, "model_for_messages", None)
    if callable(selector):
        try:
            return str(selector(messages))
        except Exception:
            pass
    return str(getattr(client, "model", type(client).__name__))


def _sanitize(value: object, key: str = "") -> object:
    if key and _SENSITIVE_KEY.search(key):
        return "[REDACTED]"
    if isinstance(value, dict):
        return {str(item_key): _sanitize(item, str(item_key)) for item_key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_sanitize(item) for item in value]
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, bytes):
        return f"[BINARY OMITTED: {len(value)} bytes]"
    if isinstance(value, str):
        if key in {"arguments", "modified_arguments"} and value.lstrip().startswith("{"):
            try:
                parsed = json.loads(value)
            except json.JSONDecodeError:
                pass
            else:
                return _sanitize(parsed)
        sanitized = _BEARER.sub("Bearer [REDACTED]", value)
        sanitized = _INLINE_SECRET.sub(r"\1\2[REDACTED]", sanitized)
        return _DATA_IMAGE.sub(
            lambda match: (
                f"data:image/{match.group(1)};base64,"
                f"[IMAGE DATA OMITTED: {len(match.group(2))} chars]"
            ),
            sanitized,
        )
    return value


def _json_default(value: object) -> str:
    return repr(value)
