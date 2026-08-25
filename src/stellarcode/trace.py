"""Scoped JSONL tracing for LLM, tool, approval, and Runtime diagnostic events."""

from __future__ import annotations

import json
import re
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from stellarcode.llm.types import (
    ChatResult,
    chat_with_optional_delta,
    current_llm_operation,
    current_llm_scope,
    normalize_chat_result,
)


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
_NON_SECRET_TOKEN_KEYS = {
    "cached_input_tokens",
    "estimated_tokens",
    "input_tokens",
    "output_tokens",
    "reasoning_tokens",
    "total_tokens",
}
TraceTarget = tuple[int, Path]
ScopedTraceTarget = tuple[str, TraceTarget]


class TraceRecorder:
    """Thread-safe, opt-in JSONL recorder for complete Agent execution traces."""

    def __init__(self, directory: str | Path) -> None:
        self.directory = Path(directory)
        self._enabled = False
        self._path: Path | None = None
        self._session_id = ""
        self._lock = threading.RLock()
        self._last_error = ""
        self._generation = 0

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
            self._generation += 1
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
            self._generation += 1
            return path

    def close(self) -> None:
        self.disable()

    def record(self, event: str, **data: object) -> None:
        with self._lock:
            if not self._enabled:
                return
            self._write(event, data)

    def capture_target(self) -> TraceTarget | None:
        """Capture the current file generation for detached/background work."""

        with self._lock:
            if not self._enabled or self._path is None:
                return None
            return self._generation, self._path

    def record_for(
        self,
        target: TraceTarget | None,
        event: str,
        **data: object,
    ) -> None:
        """Record only if the captured conversation trace is still active."""

        if target is None:
            return
        with self._lock:
            if (
                not self._enabled
                or self._path is None
                or target != (self._generation, self._path)
            ):
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


class ScopedTraceRecorder:
    """Route trace writes to the conversation in the current Runtime scope.

    A desktop project may execute several conversations concurrently.  The
    selected conversation is only a UI fallback; Agent, tool, and LLM worker
    threads carry their session id through ``llm_runtime_scope`` and therefore
    continue writing to their own trace after the user switches elsewhere.
    """

    def __init__(self, directory: str | Path) -> None:
        self.directory = Path(directory)
        self._recorders: dict[str, TraceRecorder] = {}
        self._selected_session_id = ""
        self._lock = threading.RLock()

    def select(self, session_id: str) -> None:
        with self._lock:
            self._selected_session_id = session_id

    def configure(
        self,
        session_id: str,
        enabled: bool,
        **metadata: object,
    ) -> Path | None:
        with self._lock:
            recorder = self._recorders.setdefault(
                session_id,
                TraceRecorder(self.directory),
            )
            self._selected_session_id = session_id
        return recorder.enable(**metadata) if enabled else recorder.disable()

    @property
    def enabled(self) -> bool:
        recorder = self._selected_recorder()
        return recorder.enabled if recorder is not None else False

    @property
    def path(self) -> Path | None:
        recorder = self._selected_recorder()
        return recorder.path if recorder is not None else None

    @property
    def last_error(self) -> str:
        recorder = self._selected_recorder()
        return recorder.last_error if recorder is not None else ""

    def enable(self, **metadata: object) -> Path:
        session_id = self._selected_or_scoped_session()
        if not session_id:
            raise RuntimeError("select a conversation before enabling trace recording")
        path = self.configure(session_id, True, **metadata)
        if path is None:
            raise RuntimeError("trace recorder did not create an output path")
        return path

    def disable(self) -> Path | None:
        session_id = self._selected_or_scoped_session()
        if not session_id:
            return None
        return self.configure(session_id, False)

    def record(self, event: str, **data: object) -> None:
        session_id = self._selected_or_scoped_session()
        if session_id:
            self.record_for_session(session_id, event, **data)

    def record_for_session(
        self,
        target_session_id: str,
        event: str,
        **data: object,
    ) -> None:
        """Record for one conversation without reserving ``session_id`` in data.

        Runtime events intentionally keep their owning ``session_id`` in the
        trace payload.  Using a distinct routing-parameter name prevents that
        payload field from colliding with this method's first argument.
        """
        with self._lock:
            recorder = self._recorders.get(target_session_id)
        if recorder is not None:
            recorder.record(event, **data)

    def capture_target(self) -> ScopedTraceTarget | None:
        session_id = self._selected_or_scoped_session()
        if not session_id:
            return None
        with self._lock:
            recorder = self._recorders.get(session_id)
        target = recorder.capture_target() if recorder is not None else None
        return (session_id, target) if target is not None else None

    def record_for(
        self,
        target: ScopedTraceTarget | None,
        event: str,
        **data: object,
    ) -> None:
        if target is None:
            return
        session_id, recorder_target = target
        with self._lock:
            recorder = self._recorders.get(session_id)
        if recorder is not None:
            recorder.record_for(recorder_target, event, **data)

    def status(self) -> str:
        recorder = self._selected_recorder()
        return recorder.status() if recorder is not None else "trace mode: off; file: (no trace file yet)"

    def close(self) -> None:
        with self._lock:
            recorders = list(self._recorders.values())
            self._recorders.clear()
        for recorder in recorders:
            recorder.close()

    def _selected_or_scoped_session(self) -> str:
        scoped_session_id, _task_id = current_llm_scope()
        with self._lock:
            return scoped_session_id or self._selected_session_id

    def _selected_recorder(self) -> TraceRecorder | None:
        with self._lock:
            return self._recorders.get(self._selected_session_id)


class TracingChatClient:
    """Chat client decorator that records complete requests, responses, and errors."""

    def __init__(
        self,
        delegate: object,
        recorder: TraceRecorder,
        usage_callback: Any | None = None,
    ) -> None:
        self.delegate = delegate
        self.recorder = recorder
        self.usage_callback = usage_callback
        self._prompt_snapshot_lock = threading.RLock()
        self._prompt_snapshots: dict[str, object] = {}

    def record_prompt_snapshot(self, snapshot: object) -> None:
        """Retain the latest Prompt snapshot and write only its metadata to Trace."""

        metadata_builder = getattr(snapshot, "trace_metadata", None)
        if not callable(metadata_builder):
            raise TypeError("prompt snapshot must provide trace_metadata()")
        session_id, task_id = current_llm_scope()
        key = session_id or "__default__"
        with self._prompt_snapshot_lock:
            self._prompt_snapshots[key] = snapshot
        trace_target = self.recorder.capture_target()
        self.recorder.record_for(
            trace_target,
            "prompt_assembled",
            task_id=task_id,
            prompt=metadata_builder(),
        )

    def prompt_snapshot(
        self,
        session_id: str = "",
        *,
        include_sensitive: bool = False,
    ) -> dict[str, Any] | None:
        """Return a backend-redacted snapshot for one desktop conversation."""

        key = session_id or "__default__"
        with self._prompt_snapshot_lock:
            snapshot = self._prompt_snapshots.get(key)
        serializer = getattr(snapshot, "to_dict", None)
        if not callable(serializer):
            return None
        return serializer(include_sensitive=include_sensitive)

    def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        temperature: float = 0.2,
        on_delta: Any | None = None,
    ) -> ChatResult:
        model = _request_model(self.delegate, messages)
        trace_target = self.recorder.capture_target()
        self.recorder.record_for(
            trace_target,
            "llm_request",
            model=model,
            temperature=temperature,
            messages=messages,
            tools=tools or [],
        )
        started_at = time.monotonic()
        try:
            raw_response = chat_with_optional_delta(
                self.delegate,
                messages,
                tools=tools,
                temperature=temperature,
                on_delta=on_delta,
            )
        except Exception as exc:
            self.recorder.record_for(
                trace_target,
                "llm_error",
                model=model,
                elapsed_ms=int((time.monotonic() - started_at) * 1000),
                error_type=type(exc).__name__,
                error=str(exc),
            )
            raise
        result = normalize_chat_result(
            raw_response,
            client=self.delegate,
            messages=messages,
            tools=tools,
        )
        self.recorder.record_for(
            trace_target,
            "llm_response",
            model=model,
            elapsed_ms=int((time.monotonic() - started_at) * 1000),
            response=result.message,
            usage=result.usage.to_dict(),
        )
        if self.usage_callback is not None:
            session_id, task_id = current_llm_scope()
            self.usage_callback(
                result.usage,
                result.provider,
                result.model,
                current_llm_operation(),
                session_id,
                task_id,
            )
        return result

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
    if key and key.lower() not in _NON_SECRET_TOKEN_KEYS and _SENSITIVE_KEY.search(key):
        return "[REDACTED]"
    if key in {"data_base64", "base64_data"} and isinstance(value, str):
        return f"[IMAGE DATA OMITTED: {len(value)} chars]"
    if isinstance(value, dict) and key.lower() in {"env", "environment", "headers"}:
        return {str(item_key): "[REDACTED]" for item_key in value}
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
