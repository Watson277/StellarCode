"""Atomic JSON persistence for the minimal V1 long-term memory schema."""

from __future__ import annotations

import json
import os
import threading
import uuid
from pathlib import Path

from stellarcode.memory.entry import LongTermMemoryEntry, estimate_tokens, utc_timestamp


class LongTermMemory:
    """Project-level type-free memories stored as independent JSON records."""

    def __init__(self, storage_dir: str | Path | None = None) -> None:
        env_dir = os.getenv("STELLARCODE_MEMORY_DIR")
        default_dir = Path.home() / ".stellarcode" / "memory"
        self.storage_dir = Path(storage_dir or env_dir or default_dir).resolve()
        self.storage_file = self.storage_dir / "long_term_memory.json"
        self.entries: dict[str, LongTermMemoryEntry] = {}
        self._lock = threading.RLock()
        self._warnings: list[str] = []
        self._writes_blocked = False
        self.load()

    def store(self, entry: LongTermMemoryEntry) -> bool:
        """Add one record, ignoring only an exact duplicate among active memories."""

        if not isinstance(entry, LongTermMemoryEntry):
            raise TypeError("long-term memory requires LongTermMemoryEntry")
        with self._lock:
            self._ensure_writable_locked()
            duplicate = self._active_by_content_locked(entry.content)
            if duplicate is not None:
                return False
            self.entries[entry.id] = entry
            self._save_locked()
            return True

    def active(self) -> list[LongTermMemoryEntry]:
        with self._lock:
            return [entry for entry in self.entries.values() if entry.status == "active"]

    def all(self) -> list[LongTermMemoryEntry]:
        with self._lock:
            return list(self.entries.values())

    def set_embedding(self, entry_id: str, embedding: list[float]) -> bool:
        """Persist a missing/rebuilt vector without changing the memory content."""

        with self._lock:
            self._ensure_writable_locked()
            entry = self.entries.get(entry_id)
            if entry is None:
                return False
            entry.embedding = [float(value) for value in embedding]
            entry.updated_at = utc_timestamp()
            self._save_locked()
            return True

    def apply_decision(
        self,
        action: str,
        *,
        content: str,
        embedding: list[float],
        memory_ids: list[str],
    ) -> tuple[LongTermMemoryEntry | None, bool]:
        """Apply one validated LLM decision in a single atomic JSON replacement."""

        normalized_action = action.strip().upper()
        with self._lock:
            self._ensure_writable_locked()
            targets = [self.entries.get(entry_id) for entry_id in memory_ids]
            if any(entry is None or entry.status != "active" for entry in targets):
                raise ValueError("memory decision references a non-active memory")

            if normalized_action == "IGNORE":
                return (targets[0] if targets else None), False
            if normalized_action not in {"ADD", "UPDATE", "MERGE"}:
                raise ValueError(f"unsupported memory action: {action}")

            duplicate = self._active_by_content_locked(content)
            if duplicate is not None and duplicate.id not in memory_ids:
                return duplicate, False

            now = utc_timestamp()
            for target in targets:
                assert target is not None
                target.status = "superseded"
                target.updated_at = now

            entry = LongTermMemoryEntry.create(content, embedding)
            self.entries[entry.id] = entry
            self._save_locked()
            return entry, True

    def clear(self) -> None:
        with self._lock:
            self._ensure_writable_locked()
            self.entries.clear()
            self._save_locked()

    def delete(self, entry_id: str) -> bool:
        with self._lock:
            self._ensure_writable_locked()
            if entry_id not in self.entries:
                return False
            del self.entries[entry_id]
            self._save_locked()
            return True

    def load(self) -> None:
        with self._lock:
            if not self.storage_file.exists():
                self.entries = {}
                return
            try:
                data = json.loads(self.storage_file.read_text(encoding="utf-8"))
                raw_entries = data.get("entries", []) if isinstance(data, dict) else data
                if not isinstance(raw_entries, list):
                    raise ValueError("memory entries must be a list")
                self.entries = {
                    entry.id: entry
                    for entry in (
                        LongTermMemoryEntry.from_dict(item) for item in raw_entries
                    )
                }
                self._writes_blocked = False
                # Loading the previous MemoryEntry shape is also the migration. The next
                # successful write serializes only the six V1 fields.
                if any(
                    isinstance(item, dict) and "type" in item for item in raw_entries
                ):
                    self._save_locked()
            except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
                self.entries = {}
                self._quarantine_corrupt_file_locked(exc)

    def save(self) -> None:
        with self._lock:
            self._ensure_writable_locked()
            self._save_locked()

    def ensure_storage_file(self) -> Path:
        """Materialize an empty store so desktop file reveal can resolve the path."""

        with self._lock:
            self._ensure_writable_locked()
            if not self.storage_file.exists():
                self._save_locked()
            return self.storage_file

    def warnings(self) -> tuple[str, ...]:
        with self._lock:
            return tuple(self._warnings)

    def count(self) -> int:
        with self._lock:
            return len(self.entries)

    def token_count(self) -> int:
        with self._lock:
            return sum(estimate_tokens(entry.content) for entry in self.entries.values())

    def _active_by_content_locked(self, content: str) -> LongTermMemoryEntry | None:
        normalized = content.strip().casefold()
        return next(
            (
                entry
                for entry in self.entries.values()
                if entry.status == "active" and entry.content.casefold() == normalized
            ),
            None,
        )

    def _save_locked(self) -> None:
        self._ensure_writable_locked()
        self.storage_dir.mkdir(parents=True, exist_ok=True)
        payload = {"entries": [entry.to_dict() for entry in self.entries.values()]}
        temporary = self.storage_file.with_suffix(self.storage_file.suffix + ".tmp")
        with temporary.open("w", encoding="utf-8", newline="\n") as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, self.storage_file)

    def _quarantine_corrupt_file_locked(self, error: Exception) -> None:
        quarantine = self.storage_file.with_name(
            f"{self.storage_file.stem}.corrupt-{uuid.uuid4().hex}{self.storage_file.suffix}"
        )
        try:
            os.replace(self.storage_file, quarantine)
        except OSError as quarantine_error:
            self._writes_blocked = True
            self._warn_locked(
                f"could not load {self.storage_file}: {type(error).__name__}: {error}; "
                "the original file was preserved and writes are blocked because it could not "
                f"be quarantined: {quarantine_error}"
            )
            return
        self._writes_blocked = False
        self._warn_locked(
            f"could not load {self.storage_file}: {type(error).__name__}: {error}; "
            f"the original file was preserved at {quarantine}"
        )

    def _ensure_writable_locked(self) -> None:
        if self._writes_blocked:
            raise RuntimeError(
                "long-term memory writes are blocked because the corrupt storage file "
                "could not be quarantined"
            )

    def _warn_locked(self, message: str) -> None:
        if message not in self._warnings:
            self._warnings.append(message)
