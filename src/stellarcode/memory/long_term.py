from __future__ import annotations

import json
import os
import threading
import uuid
from pathlib import Path

from stellarcode.memory.entry import MemoryEntry


class LongTermMemory:
    def __init__(self, storage_dir: str | Path | None = None) -> None:
        env_dir = os.getenv("STELLARCODE_MEMORY_DIR")
        default_dir = Path.home() / ".stellarcode" / "memory"
        self.storage_dir = Path(storage_dir or env_dir or default_dir).resolve()
        self.storage_file = self.storage_dir / "long_term_memory.json"
        self.entries: dict[str, MemoryEntry] = {}
        self._lock = threading.RLock()
        self._warnings: list[str] = []
        self._writes_blocked = False
        self.load()

    def store(self, entry: MemoryEntry) -> bool:
        with self._lock:
            self._ensure_writable_locked()
            if any(existing.content == entry.content for existing in self.entries.values()):
                return False
            self.entries[entry.id] = entry
            self._save_locked()
            return True

    def all(self) -> list[MemoryEntry]:
        with self._lock:
            return list(self.entries.values())

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
                    for entry in (MemoryEntry.from_dict(item) for item in raw_entries)
                }
                self._writes_blocked = False
            except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
                self.entries = {}
                self._quarantine_corrupt_file_locked(exc)

    def save(self) -> None:
        with self._lock:
            self._ensure_writable_locked()
            self._save_locked()

    def warnings(self) -> tuple[str, ...]:
        with self._lock:
            return tuple(self._warnings)

    def count(self) -> int:
        with self._lock:
            return len(self.entries)

    def token_count(self) -> int:
        with self._lock:
            return sum(entry.token_count for entry in self.entries.values())

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

