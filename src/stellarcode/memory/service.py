"""Project-level memory owner that shares facts while isolating conversation caches."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from stellarcode.memory.entry import MemoryEntry, MemoryType
from stellarcode.memory.long_term import LongTermMemory
from stellarcode.memory.manager import MemoryManager
from stellarcode.memory.retriever import MemoryRetriever


class ProjectMemoryService:
    """Own one long-term fact store while keeping conversation caches isolated."""

    def __init__(
        self,
        storage_dir: str | Path,
        *,
        context_window: int = 200_000,
    ) -> None:
        self.long_term = LongTermMemory(storage_dir=storage_dir)
        self.context_window = context_window
        self.retriever = MemoryRetriever()

    def create_conversation_manager(
        self,
        *,
        short_term_tokens: int | None = None,
        llm_client: Any | None = None,
    ) -> MemoryManager:
        return MemoryManager(
            short_term_tokens=short_term_tokens,
            context_window=self.context_window,
            long_term=self.long_term,
            llm_client=llm_client,
        )

    def snapshot(self, query: str = "", limit: int = 200) -> dict[str, object]:
        """Return a structured, project-scoped view of persisted memories."""

        normalized_query = query.strip()
        bounded_limit = max(1, min(int(limit), 500))
        all_entries = self.long_term.all()
        if normalized_query:
            entries = self.retriever.retrieve(
                normalized_query,
                [],
                all_entries,
                limit=bounded_limit,
            )
        else:
            entries = sorted(
                all_entries,
                key=lambda entry: entry.timestamp,
                reverse=True,
            )[:bounded_limit]
        return {
            "scope": "project",
            "entries": [entry.to_dict() for entry in entries],
            "count": len(all_entries),
            "returned_count": len(entries),
            "token_count": sum(entry.token_count for entry in all_entries),
            "storage_path": str(self.long_term.storage_file),
            "warnings": list(self.long_term.warnings()),
        }

    def save(self, content: str) -> tuple[MemoryEntry, bool]:
        """Persist a fact and return the canonical entry, including on dedupe."""

        normalized = content.strip()
        if not normalized:
            raise ValueError("memory content must not be empty")
        entry = MemoryEntry.create(normalized, MemoryType.FACT, {"source": "manual"})
        created = self.long_term.store(entry)
        if created:
            return entry, True
        existing = next(
            item for item in self.long_term.all() if item.content == normalized
        )
        return existing, False

    def delete(self, entry_id: str) -> bool:
        return self.long_term.delete(entry_id)

    def clear(self) -> None:
        self.long_term.clear()
