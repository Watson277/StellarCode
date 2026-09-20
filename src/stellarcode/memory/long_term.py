"""Transactional SQLite persistence for the six-field long-term memory schema."""

from __future__ import annotations

import json
import os
import sqlite3
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from stellarcode.memory.entry import LongTermMemoryEntry, estimate_tokens, utc_timestamp


class LongTermMemory:
    """One physical scope, with fresh reads across Runtime instances and processes."""

    def __init__(self, storage_dir: str | Path | None = None) -> None:
        default_dir = Path.home() / ".stellarcode" / "memory"
        self.storage_dir = Path(
            storage_dir or os.getenv("STELLARCODE_MEMORY_DIR") or default_dir
        ).resolve()
        self.storage_file = self.storage_dir / "long_term_memory.db"
        self._warnings: list[str] = []
        self.storage_dir.mkdir(parents=True, exist_ok=True)
        self._initialize()

    @contextmanager
    def _connection(self, *, write: bool = False) -> Iterator[sqlite3.Connection]:
        # Each operation owns its connection. SQLite serializes writers across threads
        # and processes; there is no mutable in-memory copy that can become stale.
        connection = sqlite3.connect(self.storage_file, timeout=30.0, isolation_level=None)
        try:
            connection.row_factory = sqlite3.Row
            connection.create_function("memory_casefold", 1, str.casefold, deterministic=True)
            connection.execute("PRAGMA synchronous=FULL")
            if write:
                connection.execute("BEGIN IMMEDIATE")
            yield connection
            if write:
                connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self._connection() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
        with self._connection(write=True) as connection:
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            if version > 1:
                raise RuntimeError(f"unsupported long-term memory database version: {version}")
            connection.execute("""
                CREATE TABLE IF NOT EXISTS memories (
                    id TEXT PRIMARY KEY NOT NULL,
                    content TEXT NOT NULL CHECK (length(trim(content)) > 0),
                    embedding TEXT NOT NULL DEFAULT '[]',
                    status TEXT NOT NULL CHECK (status IN ('active', 'superseded')),
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
            """)
            connection.execute("CREATE INDEX IF NOT EXISTS idx_memories_status ON memories(status)")
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_memories_updated ON memories(updated_at DESC)"
            )
            if version == 0:
                self._import_legacy(connection)
                # Import and marker commit together. Clear/delete must never cause the
                # retained JSON backup to be imported again on the next startup.
                connection.execute("PRAGMA user_version=1")

    def _import_legacy(self, connection: sqlite3.Connection) -> None:
        legacy = self.storage_dir / "long_term_memory.json"
        if not legacy.is_file():
            return
        try:
            payload = json.loads(legacy.read_text(encoding="utf-8"))
            raw = payload.get("entries") if isinstance(payload, dict) else payload
            if not isinstance(raw, list):
                raise ValueError("memory entries must be a list")
            entries = [LongTermMemoryEntry.from_dict(item) for item in raw]
            if len({entry.id for entry in entries}) != len(entries):
                raise ValueError("duplicate memory ids in legacy storage")
        except (UnicodeError, json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
            quarantine = legacy.with_name(f"long_term_memory.corrupt-{uuid.uuid4().hex}.json")
            # Preservation failures abort initialization instead of creating an empty
            # writable store over data that could not be safely recovered.
            legacy.replace(quarantine)
            self._warnings.append(f"could not load {legacy}: {exc}; preserved at {quarantine}")
            return
        for entry in entries:
            self._insert(connection, entry)
        for entry in entries:
            stored = connection.execute(
                "SELECT * FROM memories WHERE id = ?", (entry.id,)
            ).fetchone()
            if self._decode(stored).to_dict() != entry.to_dict():
                raise RuntimeError(f"memory migration verification failed: {entry.id}")
        # Preserve the original JSON byte-for-byte, including any legacy metadata.

    @staticmethod
    def _insert(connection: sqlite3.Connection, entry: LongTermMemoryEntry) -> None:
        connection.execute(
            "INSERT INTO memories (id, content, embedding, status, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                entry.id,
                entry.content,
                json.dumps(entry.embedding, allow_nan=False),
                entry.status,
                entry.created_at,
                entry.updated_at,
            ),
        )

    @staticmethod
    def _decode(row: sqlite3.Row) -> LongTermMemoryEntry:
        data = dict(row)
        data["embedding"] = json.loads(data["embedding"])
        return LongTermMemoryEntry.from_dict(data)

    @classmethod
    def _duplicate(
        cls,
        connection: sqlite3.Connection,
        content: str,
    ) -> LongTermMemoryEntry | None:
        row = connection.execute(
            "SELECT * FROM memories WHERE status = 'active' "
            "AND memory_casefold(content) = ? LIMIT 1",
            (content.strip().casefold(),),
        ).fetchone()
        return cls._decode(row) if row is not None else None

    def store(self, entry: LongTermMemoryEntry) -> bool:
        if not isinstance(entry, LongTermMemoryEntry):
            raise TypeError("long-term memory requires LongTermMemoryEntry")
        with self._connection(write=True) as connection:
            if self._duplicate(connection, entry.content) is not None:
                return False
            existing = connection.execute(
                "SELECT * FROM memories WHERE id = ?", (entry.id,)
            ).fetchone()
            if existing is not None:
                if self._decode(existing).to_dict() == entry.to_dict():
                    return False
                raise ValueError(f"memory id already exists: {entry.id}")
            self._insert(connection, entry)
            return True

    def active(self) -> list[LongTermMemoryEntry]:
        with self._connection() as connection:
            return [
                self._decode(row)
                for row in connection.execute(
                    "SELECT * FROM memories WHERE status = 'active' ORDER BY rowid"
                )
            ]

    def all(self) -> list[LongTermMemoryEntry]:
        with self._connection() as connection:
            return [
                self._decode(row)
                for row in connection.execute("SELECT * FROM memories ORDER BY rowid")
            ]

    def set_embedding(self, entry_id: str, embedding: list[float]) -> bool:
        encoded = json.dumps([float(value) for value in embedding], allow_nan=False)
        with self._connection(write=True) as connection:
            return (
                connection.execute(
                    "UPDATE memories SET embedding = ?, updated_at = ? WHERE id = ?",
                    (encoded, utc_timestamp(), entry_id),
                ).rowcount
                > 0
            )

    def apply_decision(
        self,
        action: str,
        *,
        content: str,
        embedding: list[float],
        memory_ids: list[str],
    ) -> tuple[LongTermMemoryEntry | None, bool]:
        """Validate stale targets and supersede/insert within one write transaction."""
        action = action.strip().upper()
        ids = list(dict.fromkeys(memory_ids))
        if action not in {"ADD", "IGNORE", "UPDATE", "MERGE"}:
            raise ValueError(f"unsupported memory action: {action}")
        if (
            (action == "ADD" and ids)
            or (action in {"IGNORE", "UPDATE"} and len(ids) != 1)
            or (action == "MERGE" and not ids)
        ):
            raise ValueError(f"invalid memory targets for {action}")
        with self._connection(write=True) as connection:
            targets = [
                connection.execute(
                    "SELECT * FROM memories WHERE id = ? AND status = 'active'", (entry_id,)
                ).fetchone()
                for entry_id in ids
            ]
            if any(row is None for row in targets):
                raise ValueError("memory decision references a non-active memory")
            if action == "IGNORE":
                return self._decode(targets[0]), False
            duplicate = self._duplicate(connection, content)
            if duplicate is not None and duplicate.id not in ids:
                return duplicate, False
            connection.executemany(
                "UPDATE memories SET status = 'superseded', updated_at = ? WHERE id = ?",
                [(utc_timestamp(), entry_id) for entry_id in ids],
            )
            entry = LongTermMemoryEntry.create(content, embedding)
            self._insert(connection, entry)
            return entry, True

    def clear(self) -> None:
        with self._connection(write=True) as connection:
            connection.execute("DELETE FROM memories")

    def delete(self, entry_id: str) -> bool:
        with self._connection(write=True) as connection:
            return connection.execute("DELETE FROM memories WHERE id = ?", (entry_id,)).rowcount > 0

    def ensure_storage_file(self) -> Path:
        return self.storage_file

    def warnings(self) -> tuple[str, ...]:
        return tuple(self._warnings)

    def count(self) -> int:
        with self._connection() as connection:
            return connection.execute("SELECT count(*) FROM memories").fetchone()[0]

    def token_count(self) -> int:
        with self._connection() as connection:
            return sum(
                estimate_tokens(row[0])
                for row in connection.execute("SELECT content FROM memories")
            )
