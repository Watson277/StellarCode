"""Hybrid semantic, lexical, and symbol retrieval over a project's code index."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from stellarcode.rag.embedding import EmbeddingClient, EmbeddingError
from stellarcode.rag.model import CodeRelation, IndexStats, SearchResult
from stellarcode.rag.store import VectorStore
from stellarcode.rag.tokenizer import tokenize_query


class CodeRetriever:
    def __init__(
        self,
        project_path: str | Path,
        embedding_client: EmbeddingClient | None = None,
        storage_dir: str | Path | None = None,
    ) -> None:
        self.embedding_client = embedding_client or EmbeddingClient()
        self.vector_store = VectorStore(project_path, storage_dir=storage_dir)
        self.last_semantic_error = ""

    def __enter__(self) -> "CodeRetriever":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def close(self) -> None:
        self.vector_store.close()

    def semantic_search(self, query: str, top_k: int = 5) -> list[SearchResult]:
        embedding = self.embedding_client.embed(query)
        return self.vector_store.search(embedding, top_k)

    def keyword_search(self, keyword: str) -> list[SearchResult]:
        return self.vector_store.search_by_keyword(keyword)

    def hybrid_search(self, query: str, top_k: int = 5) -> list[SearchResult]:
        if not query.strip():
            return []
        top_k = max(1, min(int(top_k), 20))
        merged: dict[str, SearchResult] = {}
        dual_match_bonused: set[str] = set()
        self.last_semantic_error = ""

        try:
            semantic_limit = max(top_k * 2, 10)
            for result in self.semantic_search(query, semantic_limit):
                _merge_result(merged, result, dual_match_bonused)
        except EmbeddingError as exc:
            self.last_semantic_error = str(exc)

        for keyword in tokenize_query(query):
            for result in self.keyword_search(keyword):
                boosted = _boost_keyword_match(result, keyword)
                _merge_result(merged, boosted, dual_match_bonused)

        ranked = []
        for result in merged.values():
            type_boost = {
                "method": 0.15,
                "class": 0.10,
            }.get(result.chunk_type, 0.0)
            ranked.append(replace(result, similarity=result.similarity + type_boost))
        ranked.sort(key=lambda item: item.similarity, reverse=True)
        return _limit_per_file(ranked, top_k, max_per_file=2)

    def get_relation_graph(self, name: str) -> list[CodeRelation]:
        return self.vector_store.get_relations(name)

    def get_stats(self) -> IndexStats:
        return self.vector_store.get_stats()


def _merge_result(
    merged: dict[str, SearchResult],
    candidate: SearchResult,
    dual_match_bonused: set[str],
) -> None:
    key = candidate.identity
    existing = merged.get(key)
    if existing is None:
        merged[key] = candidate
        return
    score = max(existing.similarity, candidate.similarity)
    if key not in dual_match_bonused:
        score += 0.1
        dual_match_bonused.add(key)
    merged[key] = replace(candidate, similarity=score)


def _boost_keyword_match(result: SearchResult, keyword: str) -> SearchResult:
    keyword_lower = keyword.lower()
    bonus = 0.0
    if keyword_lower in result.name.lower():
        bonus += 0.3
    if keyword_lower in result.file_path.lower():
        bonus += 0.1
    if keyword_lower in result.content.lower():
        bonus += 0.1
    return replace(result, similarity=result.similarity + bonus)


def _limit_per_file(
    sorted_results: list[SearchResult],
    top_k: int,
    max_per_file: int,
) -> list[SearchResult]:
    results: list[SearchResult] = []
    file_counts: dict[str, int] = {}
    for result in sorted_results:
        count = file_counts.get(result.file_path, 0)
        if count >= max_per_file:
            continue
        results.append(result)
        file_counts[result.file_path] = count + 1
        if len(results) >= top_k:
            break
    return results
