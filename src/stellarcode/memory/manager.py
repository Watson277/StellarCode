from __future__ import annotations

import threading
from pathlib import Path

from stellarcode.memory.compressor import ContextCompressor
from stellarcode.memory.conversation import ConversationMemory
from stellarcode.memory.entry import MemoryEntry, MemoryType
from stellarcode.memory.long_term import LongTermMemory
from stellarcode.memory.retriever import MemoryRetriever
from stellarcode.memory.token_budget import TokenBudget


class MemoryManager:
    def __init__(
        self,
        storage_dir: str | Path | None = None,
        short_term_tokens: int = 8192,
        context_window: int = 200_000,
    ) -> None:
        self.short_term = ConversationMemory(max_tokens=short_term_tokens)
        self.long_term = LongTermMemory(storage_dir=storage_dir)
        self.compressor = ContextCompressor()
        self.retriever = MemoryRetriever()
        self.token_budget = TokenBudget(context_window=context_window)
        self._lock = threading.RLock()

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
        with self._lock:
            if self.token_budget.needs_compression(self.short_term.token_count()):
                return self.compressor.compress(self.short_term, self.long_term)
            if self.short_term.usage_ratio() > 0.8:
                return self.compressor.compress(self.short_term, self.long_term)
            return ""

    def clear_short_term(self) -> None:
        with self._lock:
            old_entries = self.short_term.pop_old_entries_for_compression(retain_recent=0)
            for fact in self.compressor.extract_facts(old_entries):
                self.long_term.store(
                    MemoryEntry.create(fact, MemoryType.FACT, {"source": "clear"})
                )
            self.short_term.clear()

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
                    f"usage: {self.short_term.usage_ratio():.0%}, compressed summaries: "
                    f"{len(self.short_term.compressed_summaries)})",
                    f"Long-term: {len(self.long_term.entries)} entries / "
                    f"{long_tokens} tokens (file: {self.long_term.storage_file})",
                    self.token_budget.report(),
                ]
            )
