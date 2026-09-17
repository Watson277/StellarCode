"""Layered prompt recall for short-term, conversation, and user memory."""

from __future__ import annotations

import math
import re
import time
from dataclasses import dataclass
from datetime import datetime

from stellarcode.memory.entry import (
    LongTermMemoryEntry,
    MemoryEntry,
    MemoryType,
    estimate_tokens,
)


_PROMPT_CONTEXT_TYPES = frozenset({MemoryType.FACT, MemoryType.SUMMARY})
MemoryRecord = MemoryEntry | LongTermMemoryEntry


@dataclass
class ScoredMemory:
    entry: MemoryRecord
    score: float


class MemoryRetriever:
    """Retrieve local context while always exposing active user-level memories."""

    def retrieve(
        self,
        query: str,
        short_entries: list[MemoryEntry],
        long_entries: list[LongTermMemoryEntry],
        limit: int = 5,
    ) -> list[MemoryRecord]:
        query_tokens = _tokenize(query)
        scored: list[ScoredMemory] = []

        for entry in short_entries:
            score = _score_short(entry, query_tokens, source_weight=1.0)
            if score > 0:
                scored.append(ScoredMemory(entry, score))

        for entry in long_entries:
            if entry.status != "active":
                continue
            score = _score_long(entry, query_tokens, source_weight=1.2)
            if score > 0:
                scored.append(ScoredMemory(entry, score))

        scored.sort(key=lambda item: item.score, reverse=True)
        return [item.entry for item in scored[:limit]]

    def build_context_for_query(
        self,
        query: str,
        short_entries: list[MemoryEntry],
        long_entries: list[LongTermMemoryEntry],
        max_tokens: int = 500,
    ) -> str:
        query_tokens = _tokenize(query)
        prompt_short_entries = [
            entry for entry in short_entries if _eligible_short_entry(entry, query_tokens)
        ]
        prompt_long_entries = [
            entry
            for entry in long_entries
            if entry.status == "active" and _content_matches(entry.content, query_tokens)
        ]
        selected: list[str] = []
        used_tokens = 0
        for entry in self.retrieve(query, prompt_short_entries, prompt_long_entries):
            token_count = estimate_tokens(entry.content)
            if used_tokens + token_count > max_tokens:
                continue
            selected.append(f"- {entry.content}")
            used_tokens += token_count
        if not selected:
            return ""
        return "Relevant memory:\n" + "\n".join(selected)

    def build_layered_context_for_query(
        self,
        query: str,
        short_entries: list[MemoryEntry],
        conversation_entries: list[LongTermMemoryEntry],
        user_entries: list[LongTermMemoryEntry],
        max_conversation_tokens: int = 500,
    ) -> str:
        """Build a two-layer context envelope.

        Every active user memory is included without query matching or a retrieval
        budget, as required by the user-memory contract. Short-term summaries and
        conversation memories remain query-scoped and share a bounded local budget.
        """

        active_user_entries = [
            entry for entry in user_entries if entry.status == "active"
        ]
        user_lines = [f"- {entry.content}" for entry in active_user_entries]

        query_tokens = _tokenize(query)
        prompt_short_entries = [
            entry for entry in short_entries if _eligible_short_entry(entry, query_tokens)
        ]
        prompt_conversation_entries = [
            entry
            for entry in conversation_entries
            if entry.status == "active" and _content_matches(entry.content, query_tokens)
        ]
        local_lines: list[str] = []
        used_tokens = 0
        for entry in self.retrieve(
            query,
            prompt_short_entries,
            prompt_conversation_entries,
        ):
            token_count = estimate_tokens(entry.content)
            if used_tokens + token_count > max_conversation_tokens:
                continue
            local_lines.append(f"- {entry.content}")
            used_tokens += token_count

        sections: list[str] = []
        if user_lines:
            sections.append(
                "User-level long-term memory (shared across conversations):\n"
                + "\n".join(user_lines)
            )
        if local_lines:
            sections.append(
                "Relevant conversation memory:\n" + "\n".join(local_lines)
            )
        return "\n\n".join(sections)


def _eligible_short_entry(entry: MemoryEntry, query_tokens: set[str]) -> bool:
    if entry.type not in _PROMPT_CONTEXT_TYPES or not query_tokens:
        return False
    entry_tokens = _tokenize(entry.content)
    for value in entry.metadata.values():
        entry_tokens.update(_tokenize(value))
    return bool(query_tokens & entry_tokens)


def _content_matches(content: str, query_tokens: set[str]) -> bool:
    return bool(query_tokens and query_tokens & _tokenize(content))


def _score_short(
    entry: MemoryEntry,
    query_tokens: set[str],
    source_weight: float,
) -> float:
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
    type_weight = 1.15 if entry.type in _PROMPT_CONTEXT_TYPES else 1.0
    return (
        (0.65 * content_overlap + 0.2 * metadata_overlap + 0.15 * time_decay)
        * source_weight
        * type_weight
    )


def _score_long(
    entry: LongTermMemoryEntry,
    query_tokens: set[str],
    source_weight: float,
) -> float:
    if not query_tokens:
        return 0.0
    content_overlap = len(query_tokens & _tokenize(entry.content)) / len(query_tokens)
    age_hours = max((time.time() - _timestamp(entry.updated_at)) / 3600.0, 0.0)
    time_decay = math.exp(-age_hours / 168.0)
    return (0.85 * content_overlap + 0.15 * time_decay) * source_weight


def _timestamp(value: str) -> float:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return time.time()


def _tokenize(text: str) -> set[str]:
    lowered = text.lower()
    words = set(re.findall(r"[a-z0-9_]+", lowered))
    chinese_chunks = re.findall(r"[\u4e00-\u9fff]{2,}", lowered)
    for chunk in chinese_chunks:
        words.add(chunk)
        words.update(chunk[i : i + 2] for i in range(max(len(chunk) - 1, 0)))
    return {word for word in words if len(word.strip()) >= 2}
