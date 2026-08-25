"""Bounded short-term conversation memory maintained in chronological order."""

from __future__ import annotations

from collections import OrderedDict

from stellarcode.memory.entry import MemoryEntry, MemoryType


class ConversationMemory:
    def __init__(self, max_tokens: int = 8192) -> None:
        self.max_tokens = max_tokens
        self.entries: "OrderedDict[str, MemoryEntry]" = OrderedDict()
        self.compressed_summaries: list[MemoryEntry] = []

    def store(self, entry: MemoryEntry) -> None:
        self.entries[entry.id] = entry
        self._evict_oldest_if_needed()

    def all(self) -> list[MemoryEntry]:
        return [*self.compressed_summaries, *self.entries.values()]

    def recent(self, limit: int = 6) -> list[MemoryEntry]:
        return list(self.entries.values())[-limit:]

    def clear(self) -> None:
        self.entries.clear()
        self.compressed_summaries.clear()

    def token_count(self) -> int:
        return sum(entry.token_count for entry in self.all())

    def usage_ratio(self) -> float:
        if self.max_tokens <= 0:
            return 0.0
        return self.token_count() / self.max_tokens

    def add_summary(self, content: str) -> MemoryEntry:
        summary = MemoryEntry.create(content, MemoryType.SUMMARY, {"source": "compression"})
        self.compressed_summaries.append(summary)
        return summary

    def pop_old_entries_for_compression(self, retain_recent: int = 3) -> list[MemoryEntry]:
        values = list(self.entries.values())
        if len(values) <= retain_recent:
            return []
        old_entries = values[:-retain_recent]
        for entry in old_entries:
            self.entries.pop(entry.id, None)
        return old_entries

    def _evict_oldest_if_needed(self) -> None:
        while self.token_count() > self.max_tokens and len(self.entries) > 1:
            _, oldest = self.entries.popitem(last=False)
            self.compressed_summaries.append(
                MemoryEntry.create(
                    f"Evicted memory: {oldest.content[:300]}",
                    MemoryType.SUMMARY,
                    {"source": "eviction"},
                )
            )

