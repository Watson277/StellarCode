"""Durable journals and task checkpoints used to recover from Sidecar failure.

Journal records are append-only and fsynced.  Checkpoints capture the current task
intent; neither should be confused with the conversation transcript shown in the UI.
"""

from __future__ import annotations

import json
import os
import threading
import hashlib
from pathlib import Path
from typing import Any


TERMINAL_TASK_EVENTS = {"task.completed", "task.failed", "task.cancelled"}


class EventJournal:
    """Durable, per-project RuntimeEvent journal used after Sidecar reconnects."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._sequences: dict[str, int] = {}
        self._terminal_tasks: set[str] = set()
        self._repair_partial_tail()
        self._load_index()

    def sequence_snapshot(self) -> dict[str, int]:
        with self._lock:
            return dict(self._sequences)

    def append(self, event: dict[str, Any]) -> None:
        encoded = json.dumps(event, ensure_ascii=False, separators=(",", ":"))
        session_id = str(event.get("session_id") or "runtime")
        sequence = int(event.get("sequence") or 0)
        task_id = str(event.get("task_id") or "")
        with self._lock:
            # A previous process can stop after writing only part of its final
            # JSONL record. Repair that tail before appending, otherwise this
            # event would be concatenated to the fragment and both records would
            # become unrecoverable on the next restart.
            self._repair_partial_tail()
            with self.path.open("a", encoding="utf-8", newline="\n") as stream:
                stream.write(encoded + "\n")
                stream.flush()
                os.fsync(stream.fileno())
            self._sequences[session_id] = max(
                sequence,
                self._sequences.get(session_id, 0),
            )
            if task_id and event.get("type") in TERMINAL_TASK_EVENTS:
                self._terminal_tasks.add(task_id)

    def replay(
        self,
        session_id: str,
        after_sequence: int,
        *,
        limit: int = 2_000,
        event_types: set[str] | None = None,
    ) -> list[dict[str, Any]]:
        if limit < 1:
            return []
        events: list[dict[str, Any]] = []
        with self._lock:
            if not self.path.exists():
                return events
            with self.path.open("r", encoding="utf-8") as stream:
                for line in stream:
                    try:
                        event = json.loads(line)
                    except (json.JSONDecodeError, TypeError, ValueError):
                        # A process can die between writing a JSONL record and its newline.
                        # Earlier complete records remain valid and replayable.
                        continue
                    if not isinstance(event, dict):
                        continue
                    if str(event.get("session_id") or "") != session_id:
                        continue
                    try:
                        sequence = int(event.get("sequence") or 0)
                    except (TypeError, ValueError):
                        continue
                    if sequence <= after_sequence:
                        continue
                    if (
                        event_types is not None
                        and str(event.get("type") or "") not in event_types
                    ):
                        continue
                    events.append(event)
                    if len(events) >= limit:
                        break
        return events

    def task_is_terminal(self, task_id: str) -> bool:
        with self._lock:
            return task_id in self._terminal_tasks

    def _repair_partial_tail(self) -> None:
        """Make the final JSONL record restart-safe before indexing/appending.

        Normal writes always include a newline before ``fsync``. If a process
        stops earlier, the last line may be either a complete JSON object whose
        newline was not written yet or a partial JSON fragment. Preserve the
        former by adding its delimiter; truncate only the latter, keeping every
        previously delimited record intact.
        """

        if not self.path.exists():
            return
        with self.path.open("r+b") as stream:
            stream.seek(0, os.SEEK_END)
            size = stream.tell()
            if size == 0:
                return
            stream.seek(-1, os.SEEK_END)
            if stream.read(1) == b"\n":
                return

            # Scan backward in bounded chunks so a very large event journal does
            # not need to be loaded solely to isolate its final record.
            cursor = size
            last_newline = -1
            while cursor > 0 and last_newline < 0:
                chunk_start = max(0, cursor - 64 * 1024)
                stream.seek(chunk_start)
                chunk = stream.read(cursor - chunk_start)
                offset = chunk.rfind(b"\n")
                if offset >= 0:
                    last_newline = chunk_start + offset
                    break
                cursor = chunk_start

            tail_start = last_newline + 1
            stream.seek(tail_start)
            tail = stream.read(size - tail_start)
            complete = False
            try:
                complete = isinstance(json.loads(tail.decode("utf-8")), dict)
            except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError):
                complete = False

            if complete:
                stream.seek(0, os.SEEK_END)
                stream.write(b"\n")
            else:
                stream.truncate(tail_start)
            stream.flush()
            os.fsync(stream.fileno())

    def _load_index(self) -> None:
        if not self.path.exists():
            return
        with self.path.open("r", encoding="utf-8") as stream:
            for line in stream:
                try:
                    event = json.loads(line)
                    session_id = str(event.get("session_id") or "runtime")
                    sequence = int(event.get("sequence") or 0)
                except (AttributeError, json.JSONDecodeError, TypeError, ValueError):
                    continue
                self._sequences[session_id] = max(
                    sequence,
                    self._sequences.get(session_id, 0),
                )
                task_id = str(event.get("task_id") or "")
                if task_id and event.get("type") in TERMINAL_TASK_EVENTS:
                    self._terminal_tasks.add(task_id)


class TaskCheckpointStore:
    """Atomic per-task checkpoints for one project Runtime.

    Older desktop builds stored one ``active-task.json`` file. The first access
    migrates that record into the task directory so unfinished work remains
    recoverable while new builds can persist multiple conversation tasks.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.directory = self.path.parent / "tasks"
        self.directory.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._migrate_legacy()

    def load(self, task_id: str | None = None) -> dict[str, Any] | None:
        with self._lock:
            if task_id:
                return self._read(self._task_path(task_id), expected_task_id=task_id)
            values = self.load_all()
            if not values:
                return None
            return max(
                values,
                key=lambda item: str(item.get("updated_at") or item.get("started_at") or ""),
            )

    def load_all(self) -> list[dict[str, Any]]:
        with self._lock:
            values: list[dict[str, Any]] = []
            for checkpoint_path in self.directory.glob("*.json"):
                payload = self._read(checkpoint_path)
                if payload is not None:
                    values.append(payload)
            return values

    def write(self, payload: dict[str, Any]) -> None:
        task_id = str(payload.get("task_id") or "")
        if not task_id:
            raise ValueError("task checkpoint requires task_id")
        with self._lock:
            target = self._task_path(task_id)
            temporary = target.with_suffix(target.suffix + ".tmp")
            with temporary.open("w", encoding="utf-8", newline="\n") as stream:
                json.dump(payload, stream, ensure_ascii=False, indent=2)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, target)

    def update(self, task_id: str, **changes: Any) -> dict[str, Any] | None:
        with self._lock:
            payload = self.load(task_id)
            if payload is None:
                return None
            payload.update(changes)
            self.write(payload)
            return payload

    def clear(self, task_id: str) -> bool:
        with self._lock:
            target = self._task_path(task_id)
            payload = self._read(target, expected_task_id=task_id)
            if payload is None:
                return False
            try:
                target.unlink()
            except FileNotFoundError:
                pass
            return True

    def _task_path(self, task_id: str) -> Path:
        digest = hashlib.sha256(task_id.encode("utf-8")).hexdigest()
        return self.directory / f"{digest}.json"

    @staticmethod
    def _read(
        path: Path,
        *,
        expected_task_id: str | None = None,
    ) -> dict[str, Any] | None:
        if not path.exists():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            return None
        if not isinstance(payload, dict):
            return None
        if expected_task_id is not None and str(payload.get("task_id") or "") != expected_task_id:
            return None
        return payload

    def _migrate_legacy(self) -> None:
        with self._lock:
            payload = self._read(self.path)
            if payload is None:
                return
            task_id = str(payload.get("task_id") or "")
            if not task_id:
                return
            target = self._task_path(task_id)
            if not target.exists():
                self.write(payload)
            try:
                self.path.unlink()
            except FileNotFoundError:
                pass
