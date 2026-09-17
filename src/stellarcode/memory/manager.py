"""Per-conversation short-term memory over layered long-term fact stores.

The manager never holds its state lock while asking an LLM to summarize or extract facts.
This avoids a lock cycle when a traced/model callback inspects conversation state.
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

from stellarcode.memory.compressor import ContextCompressor
from stellarcode.memory.conversation import ConversationMemory
from stellarcode.memory.entry import (
    ExtractedFact,
    LongTermMemoryEntry,
    MemoryEntry,
    MemoryType,
)
from stellarcode.memory.long_term import LongTermMemory
from stellarcode.memory.reconciler import LongTermMemoryReconciler, MemoryWriteResult
from stellarcode.memory.retriever import MemoryRetriever
from stellarcode.memory.token_budget import TokenBudget, proportional_short_term_tokens
from stellarcode.rag.embedding import EmbeddingClient


class MemoryManager:
    def __init__(
        self,
        storage_dir: str | Path | None = None,
        short_term_tokens: int | None = None,
        context_window: int = 200_000,
        long_term: LongTermMemory | None = None,
        user_long_term: LongTermMemory | None = None,
        llm_client: Any | None = None,
        embedding_client: EmbeddingClient | None = None,
    ) -> None:
        self.token_budget = TokenBudget(context_window=context_window)
        resolved_short_term_tokens = (
            proportional_short_term_tokens(context_window)
            if short_term_tokens is None
            else short_term_tokens
        )
        self.short_term = ConversationMemory(max_tokens=resolved_short_term_tokens)
        self.conversation_long_term = (
            long_term
            if long_term is not None
            else LongTermMemory(storage_dir=storage_dir)
        )
        # ``long_term`` remains a compatibility alias for the conversation layer.
        self.long_term = self.conversation_long_term
        self.user_long_term = user_long_term or LongTermMemory(
            storage_dir=self.conversation_long_term.storage_dir / "user"
        )
        self.compressor = ContextCompressor(llm_client=llm_client)
        self.long_term_reconciler = LongTermMemoryReconciler(
            llm_client,
            embedding_client,
        )
        self.retriever = MemoryRetriever()
        self._lock = threading.RLock()
        self._compression_lock = threading.RLock()
        # Explicit and compression-triggered extraction share one serialized lane.
        # Unlike ``_lock``, callbacks and snapshots never acquire this lock, so it may
        # safely remain held while the extraction/reconciliation LLM calls are running.
        self._extraction_lock = threading.RLock()
        self._generation = 0

    def set_llm_client(self, llm_client: Any | None) -> None:
        self.compressor.set_llm_client(llm_client)
        self.long_term_reconciler.set_llm_client(llm_client)

    def add_user_message(self, content: str) -> None:
        with self._lock:
            self.short_term.store(
                MemoryEntry.create(content, MemoryType.CONVERSATION, {"role": "user"})
            )
        self.compress_if_needed()

    def add_assistant_message(self, content: str) -> None:
        if not content:
            return
        with self._lock:
            self.short_term.store(
                MemoryEntry.create(content, MemoryType.CONVERSATION, {"role": "assistant"})
            )
        self.compress_if_needed()

    def add_tool_result(self, tool_name: str, content: str) -> None:
        with self._lock:
            self.short_term.store(
                MemoryEntry.create(
                    content[:2000],
                    MemoryType.TOOL_RESULT,
                    {"tool": tool_name},
                )
            )
        self.compress_if_needed()

    def save_fact(
        self,
        content: str,
        *,
        scope: str = "conversation",
    ) -> LongTermMemoryEntry:
        normalized = content.strip()
        if not normalized:
            raise ValueError("memory content must not be empty")
        target = self._store_for_scope(scope)
        entry = LongTermMemoryEntry.create(
            normalized,
            self.long_term_reconciler.embed_for_manual_save(normalized),
        )
        target.store(entry)
        return next(
            item
            for item in target.active()
            if item.content.casefold() == normalized.casefold()
        )

    def build_context_for_query(self, query: str, max_tokens: int = 500) -> str:
        with self._lock:
            return self.retriever.build_layered_context_for_query(
                query,
                self.short_term.all(),
                self.conversation_long_term.all(),
                self.user_long_term.all(),
                max_conversation_tokens=max_tokens,
            )

    def compress_if_needed(self) -> str:
        """Compact one proportional batch without holding the state lock across LLM calls."""

        with self._compression_lock:
            with self._extraction_lock:
                with self._lock:
                    needs_compression = self.token_budget.needs_compression(
                        self.short_term.token_count(),
                        capacity_tokens=self.short_term.max_tokens,
                    )
                    if not needs_compression:
                        return ""

                    old_entries = self.short_term.pop_old_entries_for_compression(
                        self.compressor.retain_recent
                    )
                    if not old_entries:
                        return ""
                    generation = self._generation

                # Map, Reduce, and fact extraction all call the model. They must stay
                # outside self._lock so usage/trace callbacks can inspect this manager.
                summary = self.compressor.summarize(old_entries)

                with self._lock:
                    if self._generation == generation and summary:
                        self.short_term.add_summary(summary)

                facts = self.compressor.extract_facts(old_entries)
                self.long_term_reconciler.reconcile_scoped(
                    facts,
                    user_store=self.user_long_term,
                    conversation_store=self.conversation_long_term,
                )
                return summary

    def pending_user_message_count(self) -> int:
        """Return user messages eligible for an explicit long-term extraction."""

        with self._lock:
            return sum(
                1
                for entry in self.short_term.entries.values()
                if _is_unextracted_user_message(entry)
            )

    def extract_current_user_memories(self) -> dict[str, Any]:
        """Use the LLM to persist durable facts from pending user messages now.

        This path is independent of the short-term compression threshold. Messages are
        marked only after every extracted fact was reconciled successfully; a provider,
        parse, embedding, or write failure leaves the complete batch retryable.
        """

        with self._extraction_lock:
            with self._lock:
                candidates = [
                    MemoryEntry.from_dict(entry.to_dict())
                    for entry in self.short_term.entries.values()
                    if _is_unextracted_user_message(entry)
                ]
            if not candidates:
                return _memory_extraction_result([], [], processed_message_count=0)

            facts = self.compressor.extract_facts(candidates, strict=True)
            write_results = self.long_term_reconciler.reconcile_scoped(
                facts,
                user_store=self.user_long_term,
                conversation_store=self.conversation_long_term,
            )
            failures = [result for result in write_results if result.error]
            if failures:
                detail = "; ".join(
                    result.error or "unknown memory write error" for result in failures
                )
                raise RuntimeError(f"long-term memory reconciliation failed: {detail}")

            candidate_ids = {entry.id for entry in candidates}
            with self._lock:
                for entry_id in candidate_ids:
                    current = self.short_term.entries.get(entry_id)
                    if current is not None:
                        current.metadata["long_term_extracted"] = "true"
            return _memory_extraction_result(
                facts,
                write_results,
                processed_message_count=len(candidates),
            )

    def clear_short_term(self) -> None:
        with self._extraction_lock:
            with self._lock:
                old_entries = self.short_term.pop_old_entries_for_compression(
                    retain_recent=0
                )
                self.short_term.clear()
                self._generation += 1

            # Keep durable facts from discarded context, but do not hold the memory lock
            # while the extraction model is running.
            facts = self.compressor.extract_facts(old_entries)
            self.long_term_reconciler.reconcile_scoped(
                facts,
                user_store=self.user_long_term,
                conversation_store=self.conversation_long_term,
            )

    def short_term_snapshot(self) -> dict[str, object]:
        """Capture a completed cache state, never a half-applied compression batch."""

        with self._compression_lock:
            with self._lock:
                return self.short_term.snapshot()

    def restore_short_term(self, snapshot: dict[str, object]) -> None:
        """Restore persisted cache state without invoking compression or fact extraction."""

        with self._lock:
            self.short_term.restore(snapshot)
            self._generation += 1

    def search(
        self,
        query: str,
        limit: int = 5,
    ) -> list[MemoryEntry | LongTermMemoryEntry]:
        with self._lock:
            return self.retriever.retrieve(
                query,
                self.short_term.all(),
                self.conversation_long_term.all() + self.user_long_term.all(),
                limit=limit,
            )

    def status(self) -> str:
        with self._lock:
            short_tokens = self.short_term.token_count()
            conversation_tokens = self.conversation_long_term.token_count()
            user_tokens = self.user_long_term.token_count()
            return "\n".join(
                [
                    "Memory status:",
                    f"Short-term: {len(self.short_term.entries)} entries / "
                    f"{short_tokens} tokens (budget: {self.short_term.max_tokens}, "
                    "compression trigger: "
                    f"{self.token_budget.compression_trigger_tokens(self.short_term.max_tokens)}, "
                    f"usage: {self.short_term.usage_ratio():.0%}, compressed summaries: "
                    f"{len(self.short_term.compressed_summaries)})",
                    "Conversation long-term: "
                    f"{self.conversation_long_term.count()} entries / "
                    f"{conversation_tokens} tokens "
                    f"(file: {self.conversation_long_term.storage_file})",
                    f"User long-term: {self.user_long_term.count()} entries / "
                    f"{user_tokens} tokens (file: {self.user_long_term.storage_file})",
                    self.token_budget.report(),
                ]
            )

    def _store_for_scope(self, scope: str) -> LongTermMemory:
        normalized = scope.strip().lower()
        if normalized == "user":
            return self.user_long_term
        if normalized == "conversation":
            return self.conversation_long_term
        raise ValueError(f"unsupported memory scope: {scope!r}")


def _is_unextracted_user_message(entry: MemoryEntry) -> bool:
    return (
        entry.type is MemoryType.CONVERSATION
        and entry.metadata.get("role") == "user"
        and entry.metadata.get("long_term_extracted") != "true"
        and bool(entry.content.strip())
    )


def _memory_extraction_result(
    facts: list[ExtractedFact],
    write_results: list[MemoryWriteResult],
    *,
    processed_message_count: int,
) -> dict[str, Any]:
    changed_actions = {"ADD", "UPDATE", "MERGE"}
    changed = [result for result in write_results if result.action in changed_actions]
    return {
        "processed_message_count": processed_message_count,
        "fact_count": len(facts),
        "saved_count": len(changed),
        "ignored_count": sum(result.action == "IGNORE" for result in write_results),
        "user_memory_count": sum(
            result.action in changed_actions and result.scope == "USER"
            for result in write_results
        ),
        "conversation_memory_count": sum(
            result.action in changed_actions and result.scope == "CONVERSATION"
            for result in write_results
        ),
        "results": [
            {
                "candidate": result.candidate,
                "action": result.action,
                "scope": result.scope,
                "entry_id": result.entry_id,
            }
            for result in write_results
        ],
    }
