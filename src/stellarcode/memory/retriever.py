"""Small lexical retriever used to inject only query-relevant memories into prompts."""

from __future__ import annotations

import math
import re
import time
from dataclasses import dataclass

from stellarcode.memory.entry import MemoryEntry, MemoryType


_PROMPT_CONTEXT_TYPES = frozenset({MemoryType.FACT, MemoryType.SUMMARY})


@dataclass
class ScoredMemory:
    entry: MemoryEntry
    score: float


class MemoryRetriever:
    def retrieve(
        self,
        query: str,
        short_entries: list[MemoryEntry],
        long_entries: list[MemoryEntry],
        limit: int = 5,
    ) -> list[MemoryEntry]:
        query_tokens = _tokenize(query)
        scored: list[ScoredMemory] = []

        for entry in short_entries:
            score = _score(entry, query_tokens, source_weight=1.0)
            if score > 0:
                scored.append(ScoredMemory(entry, score))

        for entry in long_entries:
            score = _score(entry, query_tokens, source_weight=1.2)
            if score > 0:
                scored.append(ScoredMemory(entry, score))

        scored.sort(key=lambda item: item.score, reverse=True)
        return [item.entry for item in scored[:limit]]

    def build_context_for_query(
        self,
        query: str,
        short_entries: list[MemoryEntry],
        long_entries: list[MemoryEntry],
        max_tokens: int = 500,
    ) -> str:
        query_tokens = _tokenize(query)
        prompt_short_entries = [
            entry for entry in short_entries if _eligible_prompt_entry(entry, query_tokens)
        ]
        prompt_long_entries = [
            entry for entry in long_entries if _eligible_prompt_entry(entry, query_tokens)
        ]
        selected: list[str] = []
        used_tokens = 0
        for entry in self.retrieve(query, prompt_short_entries, prompt_long_entries):
            if used_tokens + entry.token_count > max_tokens:
                continue
            selected.append(f"- [{entry.type.value}] {entry.content}")
            used_tokens += entry.token_count
        if not selected:
            return ""
        return "Relevant memory:\n" + "\n".join(selected)


def _eligible_prompt_entry(entry: MemoryEntry, query_tokens: set[str]) -> bool:
    """Keep raw conversations and tool output out of the system message."""

    if entry.type not in _PROMPT_CONTEXT_TYPES or not query_tokens:
        return False
    entry_tokens = _tokenize(entry.content)
    for value in entry.metadata.values():
        entry_tokens.update(_tokenize(value))
    return bool(query_tokens & entry_tokens)


def _score(entry: MemoryEntry, query_tokens: set[str], source_weight: float) -> float:
    content_tokens = _tokenize(entry.content)
    metadata_tokens = set()
    for value in entry.metadata.values():
        metadata_tokens.update(_tokenize(value))

    if not query_tokens:
        return 0.0

    content_overlap = len(query_tokens & content_tokens) / len(query_tokens)
    metadata_overlap = len(query_tokens & metadata_tokens) / len(query_tokens)
    age_hours = max((time.time() - entry.timestamp) / 3600.0, 0.0)
    time_decay = math.exp(-age_hours / 168.0)
    type_weight = 1.15 if entry.type in {MemoryType.FACT, MemoryType.SUMMARY} else 1.0

    return (
        (0.65 * content_overlap + 0.2 * metadata_overlap + 0.15 * time_decay)
        * source_weight
        * type_weight
    )


def _tokenize(text: str) -> set[str]:
    lowered = text.lower()
    words = set(re.findall(r"[a-z0-9_]+", lowered))
    chinese_chunks = re.findall(r"[\u4e00-\u9fff]{2,}", lowered)
    for chunk in chinese_chunks:
        words.add(chunk)
        words.update(chunk[i : i + 2] for i in range(max(len(chunk) - 1, 0)))
    return {word for word in words if len(word.strip()) >= 2}
