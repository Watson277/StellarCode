from __future__ import annotations

import json
import os
from pathlib import Path

from stellarcode.memory.entry import MemoryEntry


class LongTermMemory:
    def __init__(self, storage_dir: str | Path | None = None) -> None:
        env_dir = os.getenv("STELLARCODE_MEMORY_DIR")
        default_dir = Path.home() / ".stellarcode" / "memory"
        self.storage_dir = Path(storage_dir or env_dir or default_dir).resolve()
        self.storage_file = self.storage_dir / "long_term_memory.json"
        self.entries: dict[str, MemoryEntry] = {}
        self.load()

    def store(self, entry: MemoryEntry) -> bool:
        if any(existing.content == entry.content for existing in self.entries.values()):
            return False
        self.entries[entry.id] = entry
        self.save()
        return True

    def all(self) -> list[MemoryEntry]:
        return list(self.entries.values())

    def clear(self) -> None:
        self.entries.clear()
        self.save()

    def delete(self, entry_id: str) -> bool:
        if entry_id not in self.entries:
            return False
        del self.entries[entry_id]
        self.save()
        return True

    def load(self) -> None:
        if not self.storage_file.exists():
            return
        data = json.loads(self.storage_file.read_text(encoding="utf-8"))
        raw_entries = data.get("entries", []) if isinstance(data, dict) else data
        self.entries = {
            entry.id: entry
            for entry in (MemoryEntry.from_dict(item) for item in raw_entries)
        }

    def save(self) -> None:
        self.storage_dir.mkdir(parents=True, exist_ok=True)
        payload = {"entries": [entry.to_dict() for entry in self.entries.values()]}
        self.storage_file.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def token_count(self) -> int:
        return sum(entry.token_count for entry in self.entries.values())

