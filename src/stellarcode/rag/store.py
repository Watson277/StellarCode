"""SQLite-backed vector/chunk store; persistence lives below retrieval policy."""

from __future__ import annotations

import json
import math
import os
import sqlite3
from pathlib import Path

from stellarcode.rag.model import CodeChunk, CodeRelation, IndexStats, SearchResult


class VectorStore:
    def __init__(
        self,
        project_path: str | Path,
        storage_dir: str | Path | None = None,
    ) -> None:
        self.project_path = str(Path(project_path).resolve())
        configured_dir = storage_dir or os.getenv("STELLARCODE_RAG_DIR")
        self.storage_dir = Path(configured_dir or Path.home() / ".stellarcode" / "rag").resolve()
        self.storage_dir.mkdir(parents=True, exist_ok=True)
        self.storage_file = self.storage_dir / "codebase.db"
        self.connection = sqlite3.connect(self.storage_file, timeout=30.0)
        self.connection.row_factory = sqlite3.Row
        self._init_tables()

    def __enter__(self) -> "VectorStore":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def close(self) -> None:
        self.connection.close()

    def _init_tables(self) -> None:
        self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS code_chunks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                project_path TEXT NOT NULL,
                file_path TEXT NOT NULL,
                chunk_type TEXT NOT NULL,
                name TEXT NOT NULL,
                start_line INTEGER NOT NULL DEFAULT 0,
                end_line INTEGER NOT NULL DEFAULT 0,
                content TEXT NOT NULL,
                embedding_json TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS code_relations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                project_path TEXT NOT NULL,
                from_file TEXT NOT NULL,
                from_name TEXT NOT NULL,
                to_file TEXT,
                to_name TEXT NOT NULL,
                relation_type TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE INDEX IF NOT EXISTS idx_chunks_project
                ON code_chunks(project_path);
            CREATE INDEX IF NOT EXISTS idx_chunks_file
                ON code_chunks(project_path, file_path);
            CREATE INDEX IF NOT EXISTS idx_chunks_type
                ON code_chunks(project_path, chunk_type);
            CREATE INDEX IF NOT EXISTS idx_relations_from
                ON code_relations(project_path, from_name);
            CREATE INDEX IF NOT EXISTS idx_relations_to
                ON code_relations(project_path, to_name);
            """
        )
        self.connection.commit()

    def clear_project(self) -> None:
        with self.connection:
            self.connection.execute(
                "DELETE FROM code_chunks WHERE project_path = ?",
                (self.project_path,),
            )
            self.connection.execute(
                "DELETE FROM code_relations WHERE project_path = ?",
                (self.project_path,),
            )

    def replace_project(
        self,
        entries: list[tuple[CodeChunk, list[float]]],
        relations: list[CodeRelation],
    ) -> None:
        with self.connection:
            self.connection.execute(
                "DELETE FROM code_chunks WHERE project_path = ?",
                (self.project_path,),
            )
            self.connection.execute(
                "DELETE FROM code_relations WHERE project_path = ?",
                (self.project_path,),
            )
            self._insert_chunks(entries)
            self._insert_relations(relations)

    def insert_chunks(self, entries: list[tuple[CodeChunk, list[float]]]) -> None:
        with self.connection:
            self._insert_chunks(entries)

    def _insert_chunks(self, entries: list[tuple[CodeChunk, list[float]]]) -> None:
        self.connection.executemany(
            """
            INSERT INTO code_chunks (
                project_path, file_path, chunk_type, name,
                start_line, end_line, content, embedding_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    self.project_path,
                    chunk.file_path,
                    chunk.chunk_type,
                    chunk.name,
                    chunk.start_line,
                    chunk.end_line,
                    chunk.content,
                    json.dumps(embedding, separators=(",", ":")),
                )
                for chunk, embedding in entries
            ],
        )

    def insert_relations(self, relations: list[CodeRelation]) -> None:
        with self.connection:
            self._insert_relations(relations)

    def _insert_relations(self, relations: list[CodeRelation]) -> None:
        self.connection.executemany(
            """
            INSERT INTO code_relations (
                project_path, from_file, from_name,
                to_file, to_name, relation_type
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    self.project_path,
                    relation.from_file,
                    relation.from_name,
                    relation.to_file,
                    relation.to_name,
                    relation.relation_type,
                )
                for relation in relations
            ],
        )

    def search(self, query_embedding: list[float], top_k: int) -> list[SearchResult]:
        rows = self.connection.execute(
            """
            SELECT file_path, chunk_type, name, start_line, end_line,
                   content, embedding_json
            FROM code_chunks
            WHERE project_path = ?
            """,
            (self.project_path,),
        ).fetchall()
        candidates: list[SearchResult] = []
        for row in rows:
            if not row["embedding_json"]:
                continue
            try:
                embedding = [float(value) for value in json.loads(row["embedding_json"])]
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
            candidates.append(self._row_to_result(row, _cosine_similarity(query_embedding, embedding)))
        candidates.sort(key=lambda result: result.similarity, reverse=True)
        return candidates[: max(0, top_k)]

    def search_by_keyword(self, keyword: str) -> list[SearchResult]:
        escaped = keyword.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        pattern = f"%{escaped}%"
        rows = self.connection.execute(
            """
            SELECT file_path, chunk_type, name, start_line, end_line, content
            FROM code_chunks
            WHERE project_path = ?
              AND (
                  name LIKE ? ESCAPE '\\'
                  OR file_path LIKE ? ESCAPE '\\'
                  OR content LIKE ? ESCAPE '\\'
              )
            """,
            (self.project_path, pattern, pattern, pattern),
        ).fetchall()
        return [self._row_to_result(row, 0.3) for row in rows]

    def get_relations(self, name: str) -> list[CodeRelation]:
        prefix = f"{name}.%"
        rows = self.connection.execute(
            """
            SELECT from_file, from_name, to_file, to_name, relation_type
            FROM code_relations
            WHERE project_path = ? AND (
                lower(from_name) = lower(?)
                OR lower(to_name) = lower(?)
                OR lower(from_name) LIKE lower(?)
                OR lower(to_name) LIKE lower(?)
            )
            ORDER BY relation_type, from_name, to_name
            """,
            (self.project_path, name, name, prefix, prefix),
        ).fetchall()
        return [_row_to_relation(row) for row in rows]

    def get_stats(self) -> IndexStats:
        chunk_count = self.connection.execute(
            "SELECT COUNT(*) FROM code_chunks WHERE project_path = ?",
            (self.project_path,),
        ).fetchone()[0]
        relation_count = self.connection.execute(
            "SELECT COUNT(*) FROM code_relations WHERE project_path = ?",
            (self.project_path,),
        ).fetchone()[0]
        file_count = self.connection.execute(
            "SELECT COUNT(DISTINCT file_path) FROM code_chunks WHERE project_path = ?",
            (self.project_path,),
        ).fetchone()[0]
        return IndexStats(int(chunk_count), int(relation_count), int(file_count))

    @staticmethod
    def _row_to_result(row: sqlite3.Row, similarity: float) -> SearchResult:
        return SearchResult(
            file_path=str(row["file_path"]),
            chunk_type=str(row["chunk_type"]),
            name=str(row["name"]),
            content=str(row["content"]),
            start_line=int(row["start_line"]),
            end_line=int(row["end_line"]),
            similarity=similarity,
        )


def _row_to_relation(row: sqlite3.Row) -> CodeRelation:
    return CodeRelation(
        from_file=str(row["from_file"]),
        from_name=str(row["from_name"]),
        to_file=str(row["to_file"]) if row["to_file"] is not None else None,
        to_name=str(row["to_name"]),
        relation_type=str(row["relation_type"]),
    )


def _cosine_similarity(left: list[float], right: list[float]) -> float:
    if not left or len(left) != len(right):
        return 0.0
    dot = sum(a * b for a, b in zip(left, right))
    left_norm = math.sqrt(sum(value * value for value in left))
    right_norm = math.sqrt(sum(value * value for value in right))
    if left_norm == 0.0 or right_norm == 0.0:
        return 0.0
    return dot / (left_norm * right_norm)
