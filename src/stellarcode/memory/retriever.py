"""Layered recall for conversation and user long-term memory."""

from __future__ import annotations

import math
import re
import time
from dataclasses import dataclass
from datetime import datetime

from stellarcode.memory.entry import (
    LongTermMemoryEntry,
    MemoryEntry,
    estimate_tokens,
)
from stellarcode.rag.embedding import EmbeddingClient


MemoryRecord = MemoryEntry | LongTermMemoryEntry


@dataclass
class ScoredMemory:
    entry: MemoryRecord
    score: float


class MemoryRetriever:
    """Hybrid keyword/vector recall for type-free long-term memory.

    User-level injection is handled by ``build_layered_context_for_query`` and remains
    unconditional. This retriever ranks conversation memories with lexical overlap,
    embedding similarity, and a small recency signal. If embedding generation fails,
    retrieval degrades to keyword matching instead of blocking the agent run.
    """

    KEYWORD_WEIGHT = 0.40
    VECTOR_WEIGHT = 0.50
    RECENCY_WEIGHT = 0.10
    MIN_VECTOR_SIMILARITY = 0.25

    def __init__(self, embedding_client: EmbeddingClient | None = None) -> None:
        self.embedding_client = embedding_client or EmbeddingClient()

    def retrieve(
        self,
        query: str,
        long_entries: list[LongTermMemoryEntry],
        limit: int = 5,
    ) -> list[MemoryRecord]:
        query_tokens = _tokenize(query)
        query_embedding = self._embed_query(query)
        scored: list[ScoredMemory] = []

        for entry in long_entries:
            if entry.status != "active":
                continue
            keyword_score = _keyword_overlap(entry.content, query_tokens)
            vector_score = _cosine_similarity(query_embedding, entry.embedding)
            if keyword_score <= 0 and vector_score < self.MIN_VECTOR_SIMILARITY:
                continue
            score = _score_long(
                entry,
                keyword_score=keyword_score,
                vector_score=vector_score,
                vector_available=bool(query_embedding and entry.embedding),
            )
            if score > 0.0:
                scored.append(ScoredMemory(entry, score))

        scored.sort(key=lambda item: item.score, reverse=True)
        return [item.entry for item in scored[:limit]]

    def build_context_for_query(
        self,
        query: str,
        long_entries: list[LongTermMemoryEntry],
        max_tokens: int = 500,
    ) -> str:
        selected: list[str] = []
        used_tokens = 0
        for entry in self.retrieve(query, long_entries):
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
        conversation_entries: list[LongTermMemoryEntry],
        user_entries: list[LongTermMemoryEntry],
        max_conversation_tokens: int = 500,
    ) -> str:
        """Build a two-layer context envelope.

        Every active user memory is included without query matching or a retrieval
        budget. Conversation memories remain query-scoped with a bounded local budget.
        """

        active_user_entries = [
            entry for entry in user_entries if entry.status == "active"
        ]
        user_lines = [f"- {entry.content}" for entry in active_user_entries]

        local_lines: list[str] = []
        used_tokens = 0
        for entry in self.retrieve(
            query,
            conversation_entries,
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

    def _embed_query(self, query: str) -> list[float]:
        if not query.strip():
            return []
        try:
            return self.embedding_client.embed(query)
        except Exception:
            return []


def _score_long(
    entry: LongTermMemoryEntry,
    *,
    keyword_score: float,
    vector_score: float,
    vector_available: bool,
) -> float:
    age_hours = max((time.time() - _timestamp(entry.updated_at)) / 3600.0, 0.0)
    time_decay = math.exp(-age_hours / 168.0)
    if not vector_available:
        # Preserve useful keyword ranking when an embedding endpoint is unavailable or
        # a legacy record has not received an embedding yet.
        return 0.90 * keyword_score + 0.10 * time_decay
    normalized_vector = max(0.0, min(1.0, vector_score))
    return (
        MemoryRetriever.KEYWORD_WEIGHT * keyword_score
        + MemoryRetriever.VECTOR_WEIGHT * normalized_vector
        + MemoryRetriever.RECENCY_WEIGHT * time_decay
    )


def _keyword_overlap(content: str, query_tokens: set[str]) -> float:
    if not query_tokens:
        return 0.0
    return len(query_tokens & _tokenize(content)) / len(query_tokens)


def _cosine_similarity(left: list[float], right: list[float]) -> float:
    if not left or not right or len(left) != len(right):
        return 0.0
    left_norm = math.sqrt(sum(value * value for value in left))
    right_norm = math.sqrt(sum(value * value for value in right))
    denominator = left_norm * right_norm
    if denominator == 0.0:
        return 0.0
    return sum(a * b for a, b in zip(left, right)) / denominator


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
