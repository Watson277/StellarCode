"""Long-term memory services; model messages remain the sole working context."""

from __future__ import annotations

import threading
import uuid
from pathlib import Path
from typing import Any, Callable

from stellarcode.memory.compressor import ContextCompressor
from stellarcode.memory.entry import (
    ExtractedFact,
    LongTermMemoryEntry,
    MemoryEntry,
    MemoryType,
)
from stellarcode.memory.long_term import LongTermMemory
from stellarcode.memory.reconciler import LongTermMemoryReconciler, MemoryWriteResult
from stellarcode.memory.retriever import MemoryRetriever
from stellarcode.memory.token_budget import TokenBudget
from stellarcode.rag.embedding import EmbeddingClient
from stellarcode.prompt.context_messages import context_kind


class MemoryManager:
    def __init__(
        self,
        storage_dir: str | Path | None = None,
        context_window: int = 200_000,
        long_term: LongTermMemory | None = None,
        user_long_term: LongTermMemory | None = None,
        llm_client: Any | None = None,
        embedding_client: EmbeddingClient | None = None,
    ) -> None:
        self.token_budget = TokenBudget(context_window=context_window)
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
        self.embedding_client = embedding_client or EmbeddingClient()
        self.long_term_reconciler = LongTermMemoryReconciler(
            llm_client,
            self.embedding_client,
        )
        self.retriever = MemoryRetriever(self.embedding_client)
        self._lock = threading.RLock()
        self._extraction_lock = threading.RLock()
        self._message_source: Callable[[], list[dict[str, Any]]] = lambda: []
        self._processed_ids: set[str] = set()
        self.message_source_bound = False

    def bind_message_source(self, source: Callable[[], list[dict[str, Any]]]) -> None:
        """Read original user messages from the owning conversation, without copying history."""
        self._message_source = source
        self.message_source_bound = True

    def extraction_snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {"processed_ids": sorted(self._processed_ids)}

    def restore_extraction(self, snapshot: dict[str, Any]) -> None:
        with self._lock:
            self._processed_ids = {
                value for value in snapshot.get("processed_ids", []) if isinstance(value, str)
            }

    def reset_extraction(self) -> None:
        with self._extraction_lock, self._lock:
            self._processed_ids.clear()

    def _pending_entries(self) -> list[MemoryEntry]:
        entries = []
        for message in list(self._message_source()):
            if message.get("role") != "user":
                continue
            content = message.get("_stellarcode_original_user_text", message.get("content"))
            if not isinstance(content, str) or not content.strip():
                continue
            # Context envelopes and tool-image messages are not user-authored facts.
            if context_kind(message) is not None:
                continue
            # Distinct turns may repeat the same preference after an intervening change.
            # Give model messages an internal ID; it is stripped from provider requests.
            identifier = str(message.get("id") or message.setdefault(
                "_stellarcode_memory_id", uuid.uuid4().hex
            ))
            if identifier in self._processed_ids:
                continue
            entries.append(MemoryEntry(
                id=identifier, content=content, type=MemoryType.CONVERSATION,
                metadata={"role": "user"},
            ))
        return entries

    def on_context_compacted(self) -> None:
        """Automatic extraction is best effort; a failed batch remains retryable."""
        try:
            self.extract_current_user_memories()
        except Exception:
            pass

    def set_llm_client(self, llm_client: Any | None) -> None:
        self.compressor.set_llm_client(llm_client)
        self.long_term_reconciler.set_llm_client(llm_client)

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
                self.conversation_long_term.all(),
                self.user_long_term.all(),
                max_conversation_tokens=max_tokens,
            )

    def pending_user_message_count(self) -> int:
        """Return user messages eligible for an explicit long-term extraction."""

        with self._lock:
            return len(self._pending_entries())

    def extract_current_user_memories(self) -> dict[str, Any]:
        """Use the LLM to persist durable facts from pending user messages now.

        This path can run explicitly or after model-context compression. Messages are
        marked only after every extracted fact was reconciled successfully; a provider,
        parse, embedding, or write failure leaves the complete batch retryable.
        """

        with self._extraction_lock:
            with self._lock:
                candidates = self._pending_entries()
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
                self._processed_ids.update(candidate_ids)
            return _memory_extraction_result(
                facts,
                write_results,
                processed_message_count=len(candidates),
            )

    def search(
        self,
        query: str,
        limit: int = 5,
    ) -> list[MemoryEntry | LongTermMemoryEntry]:
        with self._lock:
            return self.retriever.retrieve(
                query,
                self.conversation_long_term.all() + self.user_long_term.all(),
                limit=limit,
            )

    def status(self) -> str:
        with self._lock:
            conversation_tokens = self.conversation_long_term.token_count()
            user_tokens = self.user_long_term.token_count()
            return "\n".join(
                [
                    "Memory status:",
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
