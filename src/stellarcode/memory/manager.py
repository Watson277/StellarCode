"""Per-conversation short-term memory over a project-shared long-term fact store.

The manager never holds its state lock while asking an LLM to summarize or extract facts.
This avoids a lock cycle when a traced/model callback inspects conversation state.
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

from stellarcode.memory.compressor import ContextCompressor
from stellarcode.memory.conversation import ConversationMemory
from stellarcode.memory.entry import MemoryEntry, MemoryType
from stellarcode.memory.long_term import LongTermMemory
from stellarcode.memory.retriever import MemoryRetriever
from stellarcode.memory.token_budget import TokenBudget, proportional_short_term_tokens


class MemoryManager:
    def __init__(
        self,
        storage_dir: str | Path | None = None,
        short_term_tokens: int | None = None,
        context_window: int = 200_000,
        long_term: LongTermMemory | None = None,
        llm_client: Any | None = None,
    ) -> None:
        self.token_budget = TokenBudget(context_window=context_window)
        resolved_short_term_tokens = (
            proportional_short_term_tokens(context_window)
            if short_term_tokens is None
            else short_term_tokens
        )
        self.short_term = ConversationMemory(max_tokens=resolved_short_term_tokens)
        self.long_term = (
            long_term
            if long_term is not None
            else LongTermMemory(storage_dir=storage_dir)
        )
        self.compressor = ContextCompressor(llm_client=llm_client)
        self.retriever = MemoryRetriever()
        self._lock = threading.RLock()
        self._compression_lock = threading.Lock()
        self._generation = 0

    def set_llm_client(self, llm_client: Any | None) -> None:
        self.compressor.set_llm_client(llm_client)

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

    def save_fact(self, content: str) -> MemoryEntry:
        with self._lock:
            entry = MemoryEntry.create(content, MemoryType.FACT, {"source": "manual"})
            self.long_term.store(entry)
            return entry

    def build_context_for_query(self, query: str, max_tokens: int = 500) -> str:
        with self._lock:
            return self.retriever.build_context_for_query(
                query,
                self.short_term.all(),
                self.long_term.all(),
                max_tokens=max_tokens,
            )

    def compress_if_needed(self) -> str:
        """Compact one proportional batch without holding the state lock across LLM calls."""

        with self._compression_lock:
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

            # Map, Reduce, and fact extraction all call the model. They must stay outside
            # self._lock so usage/trace callbacks can safely inspect this manager.
            summary = self.compressor.summarize(old_entries)

            with self._lock:
                if self._generation == generation and summary:
                    self.short_term.add_summary(summary)

            facts = self.compressor.extract_facts(old_entries)
            for fact in facts:
                self.long_term.store(
                    MemoryEntry.create(
                        fact,
                        MemoryType.FACT,
                        {"source": "compression-llm"},
                    )
                )
            return summary

    def clear_short_term(self) -> None:
        with self._lock:
            old_entries = self.short_term.pop_old_entries_for_compression(retain_recent=0)
            self.short_term.clear()
            self._generation += 1

        # Keep durable facts from discarded context, but do not hold the memory lock
        # while the extraction model is running.
        for fact in self.compressor.extract_facts(old_entries):
            self.long_term.store(
                MemoryEntry.create(
                    fact,
                    MemoryType.FACT,
                    {"source": "clear-llm"},
                )
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

    def search(self, query: str, limit: int = 5) -> list[MemoryEntry]:
        with self._lock:
            return self.retriever.retrieve(
                query,
                self.short_term.all(),
                self.long_term.all(),
                limit=limit,
            )

    def status(self) -> str:
        with self._lock:
            short_tokens = self.short_term.token_count()
            long_tokens = self.long_term.token_count()
            return "\n".join(
                [
                    "Memory status:",
                    f"Short-term: {len(self.short_term.entries)} entries / "
                    f"{short_tokens} tokens (budget: {self.short_term.max_tokens}, "
                    "compression trigger: "
                    f"{self.token_budget.compression_trigger_tokens(self.short_term.max_tokens)}, "
                    f"usage: {self.short_term.usage_ratio():.0%}, compressed summaries: "
                    f"{len(self.short_term.compressed_summaries)})",
                    f"Long-term: {self.long_term.count()} entries / "
                    f"{long_tokens} tokens (file: {self.long_term.storage_file})",
                    self.token_budget.report(),
                ]
            )
