from __future__ import annotations

import json
import threading
import uuid
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any

from stellarcode.runtime.recovery import EventJournal


PROTOCOL_VERSION = 1


class RuntimeEventEmitter:
    """Builds ordered RuntimeEvent envelopes and writes transport-neutral messages."""

    def __init__(
        self,
        writer: Callable[[dict[str, Any]], None],
        journal: EventJournal | None = None,
    ) -> None:
        self._writer = writer
        self._sequences: dict[str, int] = {}
        self._journal = journal
        self._lock = threading.RLock()
        if journal is not None:
            self._sequences = journal.sequence_snapshot()

    def attach_journal(self, journal: EventJournal | None) -> None:
        """Switch projects and continue each persisted session sequence."""

        with self._lock:
            runtime_sequence = self._sequences.get("runtime", 0)
            self._journal = journal
            self._sequences = journal.sequence_snapshot() if journal is not None else {}
            if runtime_sequence:
                self._sequences["runtime"] = max(
                    runtime_sequence,
                    self._sequences.get("runtime", 0),
                )

    def replay(
        self,
        session_id: str,
        after_sequence: int,
        *,
        limit: int = 2_000,
        event_types: set[str] | None = None,
    ) -> list[dict[str, Any]]:
        with self._lock:
            journal = self._journal
        if journal is None:
            return []
        return journal.replay(
            session_id,
            after_sequence,
            limit=limit,
            event_types=event_types,
        )

    def emit(
        self,
        event_type: str,
        data: dict[str, Any],
        *,
        session_id: str = "runtime",
        task_id: str | None = None,
    ) -> dict[str, Any]:
        with self._lock:
            sequence = self._sequences.get(session_id, 0) + 1
            self._sequences[session_id] = sequence
            message: dict[str, Any] = {
                "kind": "event",
                "protocol_version": PROTOCOL_VERSION,
                "event_id": f"evt-{uuid.uuid4().hex}",
                "session_id": session_id,
                "sequence": sequence,
                "timestamp": datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace(
                    "+00:00", "Z"
                ),
                "type": event_type,
                "data": data,
            }
            if task_id:
                message["task_id"] = task_id
            if self._journal is not None:
                # Persist before transport delivery. If stdout breaks immediately after
                # this point the desktop can request the missing event by sequence.
                self._journal.append(message)
            self._writer(message)
            return message


def response(
    request_id: str,
    *,
    result: dict[str, Any] | None = None,
    error_code: str | None = None,
    error_message: str | None = None,
) -> dict[str, Any]:
    message: dict[str, Any] = {
        "kind": "response",
        "protocol_version": PROTOCOL_VERSION,
        "request_id": request_id,
        "ok": error_code is None,
    }
    if error_code is None:
        message["result"] = result or {}
    else:
        message["error"] = {
            "code": error_code,
            "message": error_message or error_code,
        }
    return message


class JsonLineWriter:
    def __init__(self, stream: Any) -> None:
        self._stream = stream
        self._lock = threading.Lock()

    def __call__(self, message: dict[str, Any]) -> None:
        encoded = json.dumps(message, ensure_ascii=False, separators=(",", ":"))
        with self._lock:
            self._stream.write(encoded + "\n")
            self._stream.flush()
