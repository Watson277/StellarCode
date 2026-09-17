"""Layered long-term memory ownership for one project Runtime."""

from __future__ import annotations

import re
import threading
from pathlib import Path
from typing import Any

from stellarcode.memory.entry import LongTermMemoryEntry, estimate_tokens
from stellarcode.memory.long_term import LongTermMemory
from stellarcode.memory.manager import MemoryManager
from stellarcode.memory.reconciler import LongTermMemoryReconciler
from stellarcode.memory.retriever import MemoryRetriever
from stellarcode.rag.embedding import EmbeddingClient


MEMORY_SCOPES = frozenset({"user", "conversation"})


class ProjectMemoryService:
    """Share user memory globally while isolating each conversation's store.

    The class name is retained for Runtime API compatibility. Scope is represented by
    the physical store, never by an extra field on ``LongTermMemoryEntry``.
    """

    def __init__(
        self,
        storage_dir: str | Path,
        *,
        context_window: int = 200_000,
        embedding_client: EmbeddingClient | None = None,
        user_long_term: LongTermMemory | None = None,
    ) -> None:
        self.storage_dir = Path(storage_dir).resolve()
        self.context_window = context_window
        self.retriever = MemoryRetriever()
        self.embedding_client = embedding_client or EmbeddingClient()
        self.user_long_term = user_long_term or LongTermMemory(
            self.storage_dir / "user"
        )
        self._conversation_stores: dict[str, LongTermMemory] = {}
        self._lock = threading.RLock()

    def migrate_legacy_project_memory(
        self,
        conversation_ids: list[str],
    ) -> int:
        """Copy the removed project-wide store into every pre-existing conversation.

        Project memory had no safe equivalent in the two-layer model. Copying it into
        conversations preserves prior behavior for those conversations without leaking
        project facts into the new cross-project user scope. The source file is retained
        for recovery and a marker makes the migration idempotent.
        """

        identifiers = list(dict.fromkeys(_safe_conversation_id(value) for value in conversation_ids))
        legacy_file = self.storage_dir / "long_term_memory.json"
        marker = self.storage_dir / ".layered-memory-v2-migrated"
        with self._lock:
            if marker.exists() or not legacy_file.is_file() or not identifiers:
                return 0
            legacy_store = LongTermMemory(self.storage_dir)
            legacy_entries = legacy_store.all()
            copied = 0
            for identifier in identifiers:
                target = self.conversation_store(identifier)
                for entry in legacy_entries:
                    clone = LongTermMemoryEntry.from_dict(entry.to_dict())
                    if target.store(clone):
                        copied += 1
            self.storage_dir.mkdir(parents=True, exist_ok=True)
            marker.write_text(
                "Legacy project memory was copied into existing conversation stores.\n",
                encoding="utf-8",
            )
            return copied

    def create_conversation_manager(
        self,
        conversation_id: str = "default",
        *,
        short_term_tokens: int | None = None,
        llm_client: Any | None = None,
    ) -> MemoryManager:
        return MemoryManager(
            short_term_tokens=short_term_tokens,
            context_window=self.context_window,
            long_term=self.conversation_store(conversation_id),
            user_long_term=self.user_long_term,
            llm_client=llm_client,
            embedding_client=self.embedding_client,
        )

    def conversation_store(self, conversation_id: str) -> LongTermMemory:
        identifier = _safe_conversation_id(conversation_id)
        with self._lock:
            store = self._conversation_stores.get(identifier)
            if store is None:
                store = LongTermMemory(
                    self.storage_dir / "conversations" / identifier
                )
                self._conversation_stores[identifier] = store
            return store

    def snapshot(
        self,
        conversation_id: str = "default",
        *,
        scope: str = "user",
        query: str = "",
        limit: int = 200,
    ) -> dict[str, object]:
        """Return one scope for management without changing persisted entries."""

        normalized_scope = _normalize_scope(scope)
        store = self._store(normalized_scope, conversation_id)
        # The management UI exposes a Reveal action. Tauri's reveal API requires the
        # target to exist, so an empty store must be materialized before returning it.
        store.ensure_storage_file()
        normalized_query = query.strip()
        bounded_limit = max(1, min(int(limit), 500))
        all_entries = store.all()
        if normalized_query:
            entries = self.retriever.retrieve(
                normalized_query,
                [],
                store.active(),
                limit=bounded_limit,
            )
        else:
            entries = sorted(
                all_entries,
                key=lambda entry: entry.updated_at,
                reverse=True,
            )[:bounded_limit]
        return {
            "scope": normalized_scope,
            "entries": [entry.to_dict() for entry in entries],
            "count": len(all_entries),
            "returned_count": len(entries),
            "token_count": sum(estimate_tokens(entry.content) for entry in all_entries),
            "storage_path": str(store.storage_file),
            "warnings": list(store.warnings()),
        }

    def save(
        self,
        content: str,
        *,
        conversation_id: str = "default",
        scope: str = "user",
    ) -> tuple[LongTermMemoryEntry, bool]:
        """Persist an explicit fact in the selected physical store."""

        normalized = content.strip()
        if not normalized:
            raise ValueError("memory content must not be empty")
        normalized_scope = _normalize_scope(scope)
        store = self._store(normalized_scope, conversation_id)
        embedding = LongTermMemoryReconciler(
            None,
            self.embedding_client,
        ).embed_for_manual_save(normalized)
        entry = LongTermMemoryEntry.create(normalized, embedding)
        created = store.store(entry)
        if created:
            return entry, True
        existing = next(
            item
            for item in store.active()
            if item.content.casefold() == normalized.casefold()
        )
        return existing, False

    def delete(
        self,
        entry_id: str,
        *,
        conversation_id: str = "default",
        scope: str = "user",
    ) -> bool:
        return self._store(_normalize_scope(scope), conversation_id).delete(entry_id)

    def clear(
        self,
        *,
        conversation_id: str = "default",
        scope: str = "user",
    ) -> None:
        self._store(_normalize_scope(scope), conversation_id).clear()

    def _store(self, scope: str, conversation_id: str) -> LongTermMemory:
        if scope == "user":
            return self.user_long_term
        return self.conversation_store(conversation_id)


def _normalize_scope(scope: str) -> str:
    normalized = scope.strip().lower()
    if normalized not in MEMORY_SCOPES:
        raise ValueError(f"unsupported memory scope: {scope!r}")
    return normalized


def _safe_conversation_id(conversation_id: str) -> str:
    normalized = conversation_id.strip()
    if not normalized:
        raise ValueError("conversation_id must not be empty")
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,160}", normalized):
        raise ValueError("conversation_id contains unsupported path characters")
    return normalized
