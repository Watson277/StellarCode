from __future__ import annotations

from stellarcode.memory.conversation import ConversationMemory
from stellarcode.memory.entry import MemoryEntry, MemoryType
from stellarcode.memory.long_term import LongTermMemory


class ContextCompressor:
    def __init__(self, retain_recent: int = 3) -> None:
        self.retain_recent = retain_recent

    def compress(self, memory: ConversationMemory, long_term: LongTermMemory) -> str:
        old_entries = memory.pop_old_entries_for_compression(self.retain_recent)
        if not old_entries:
            return ""

        summary = self._summarize(old_entries)
        memory.add_summary(summary)

        for fact in self.extract_facts(old_entries):
            long_term.store(MemoryEntry.create(fact, MemoryType.FACT, {"source": "compression"}))
        return summary

    def extract_facts(self, entries: list[MemoryEntry]) -> list[str]:
        facts: list[str] = []
        markers = ["记住", "偏好", "使用", "项目", "配置", "JDK", "Python", "Maven"]
        for entry in entries:
            if entry.type not in {MemoryType.CONVERSATION, MemoryType.SUMMARY}:
                continue
            content = entry.content.strip()
            if any(marker in content for marker in markers):
                facts.append(content[:500])
        return _dedupe(facts)

    def _summarize(self, entries: list[MemoryEntry]) -> str:
        parts = []
        for entry in entries:
            text = entry.content.replace("\n", " ").strip()
            if text:
                parts.append(f"{entry.type.value}: {text[:180]}")
        return "Compressed conversation summary: " + " | ".join(parts)


def _dedupe(values: list[str]) -> list[str]:
    seen = set()
    result = []
    for value in values:
        if value not in seen:
            seen.add(value)
            result.append(value)
    return result

