from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from stellarcode.rag.embedding import EmbeddingClient
from stellarcode.rag.index import CodeIndex
from stellarcode.rag.model import CodeRelation, IndexResult, IndexStats, SearchResult
from stellarcode.rag.retriever import CodeRetriever


class RagService:
    def __init__(
        self,
        project_path: str | Path,
        storage_dir: str | Path | None = None,
        embedding_client: EmbeddingClient | None = None,
    ) -> None:
        self.workspace_path = Path(project_path).resolve()
        self.project_path = self.workspace_path
        self.storage_dir = storage_dir
        self.embedding_client = embedding_client or EmbeddingClient()

    def index(
        self,
        index_path: str | Path | None = None,
        progress_callback: Callable[[str], None] | None = None,
    ) -> IndexResult:
        target = self._resolve_index_target(index_path)
        project_path = target.parent if target.is_file() else target
        indexer = CodeIndex(
            project_path=project_path,
            embedding_client=self.embedding_client,
            storage_dir=self.storage_dir,
            progress_callback=progress_callback,
        )
        result = indexer.index(target if target.is_file() else None)
        if target.exists():
            self.project_path = project_path
        return result

    def search(self, query: str, top_k: int = 5) -> list[SearchResult]:
        with CodeRetriever(
            self.project_path,
            embedding_client=self.embedding_client,
            storage_dir=self.storage_dir,
        ) as retriever:
            return retriever.hybrid_search(query, top_k)

    def graph(self, name: str) -> list[CodeRelation]:
        with CodeRetriever(
            self.project_path,
            embedding_client=self.embedding_client,
            storage_dir=self.storage_dir,
        ) as retriever:
            return retriever.get_relation_graph(name)

    def stats(self) -> IndexStats:
        with CodeRetriever(
            self.project_path,
            embedding_client=self.embedding_client,
            storage_dir=self.storage_dir,
        ) as retriever:
            return retriever.get_stats()

    def _resolve_index_target(self, index_path: str | Path | None) -> Path:
        if index_path is None or not str(index_path).strip() or str(index_path).strip() == ".":
            return self.workspace_path
        raw = Path(index_path).expanduser()
        if raw.is_absolute():
            return raw.resolve()
        return (self.workspace_path / raw).resolve()
