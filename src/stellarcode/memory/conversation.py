"""Bounded short-term conversation memory maintained in chronological order."""

from __future__ import annotations

from collections import OrderedDict

from stellarcode.memory.entry import MemoryEntry, MemoryType


SHORT_TERM_MEMORY_SCHEMA_VERSION = 1


class ConversationMemory:
    def __init__(self, max_tokens: int) -> None:
        if max_tokens <= 0:
            raise ValueError("max_tokens must be greater than 0")
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

    def snapshot(self) -> dict[str, object]:
        """Return an exact, JSON-serializable view of the current memory state."""

        return {
            "schema_version": SHORT_TERM_MEMORY_SCHEMA_VERSION,
            # This value is retained for diagnostics. Restore deliberately keeps the
            # capacity selected by the current model/runtime configuration.
            "max_tokens": self.max_tokens,
            "entries": [entry.to_dict() for entry in self.entries.values()],
            "compressed_summaries": [
                entry.to_dict() for entry in self.compressed_summaries
            ],
        }

    def restore(self, snapshot: dict[str, object]) -> None:
        """Restore a snapshot without eviction, compression, or other side effects."""

        if not isinstance(snapshot, dict):
            raise TypeError("short-term memory snapshot must be an object")
        if snapshot.get("schema_version") != SHORT_TERM_MEMORY_SCHEMA_VERSION:
            raise ValueError("unsupported short-term memory snapshot schema")

        raw_entries = snapshot.get("entries", [])
        raw_summaries = snapshot.get("compressed_summaries", [])
        if not isinstance(raw_entries, list) or not isinstance(raw_summaries, list):
            raise ValueError("short-term memory entries and summaries must be lists")

        entries = [MemoryEntry.from_dict(item) for item in raw_entries]
        summaries = [MemoryEntry.from_dict(item) for item in raw_summaries]
        if any(entry.type is not MemoryType.SUMMARY for entry in summaries):
            raise ValueError("compressed short-term memories must have SUMMARY type")

        identifiers = [entry.id for entry in [*summaries, *entries]]
        if len(identifiers) != len(set(identifiers)):
            raise ValueError("short-term memory snapshot contains duplicate entry ids")

        self.entries.clear()
        self.compressed_summaries.clear()
        self.entries.update((entry.id, entry) for entry in entries)
        self.compressed_summaries.extend(summaries)

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
        if retain_recent < 0:
            raise ValueError("retain_recent must be greater than or equal to 0")
        if retain_recent > 0 and len(values) <= retain_recent:
            if not self.compressed_summaries:
                return []
            previous_summaries = list(self.compressed_summaries)
            self.compressed_summaries.clear()
            return previous_summaries
        previous_summaries = list(self.compressed_summaries)
        entries_to_remove = values if retain_recent == 0 else values[:-retain_recent]
        old_entries = [*previous_summaries, *entries_to_remove]
        self.compressed_summaries.clear()
        for entry in entries_to_remove:
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

