from __future__ import annotations

from pathlib import Path

from stellarcode.rag import (
    CodeAnalyzer,
    CodeChunk,
    CodeChunker,
    CodeIndex,
    CodeRelation,
    CodeRetriever,
    EmbeddingClient,
    RagService,
    RagSourceStore,
    SearchResultFormatter,
    VectorStore,
    tokenize_query,
)
from stellarcode.tools import build_default_registry


SAMPLE_CODE = """\
import project.helpers


class BaseService:
    pass


class UserService(BaseService):
    def authenticate(self, username: str) -> bool:
        return self.lookup_user(username) is not None

    def lookup_user(self, username: str):
        return project.helpers.find_user(username)


def build_service() -> UserService:
    return UserService()
"""


class StubEmbeddingClient(EmbeddingClient):
    def __init__(self, vector: list[float] | None = None) -> None:
        super().__init__(provider="local")
        self.vector = vector or [1.0, 0.0]

    def embed(self, text: str | None) -> list[float]:
        return list(self.vector) if text else []


def test_python_ast_chunker_extracts_classes_methods_and_functions(tmp_path: Path):
    source = tmp_path / "service.py"
    source.write_text(SAMPLE_CODE, encoding="utf-8")

    chunks = CodeChunker().chunk_file(source, display_path="service.py")

    assert any(chunk.chunk_type == "class" and chunk.name == "UserService" for chunk in chunks)
    assert any(
        chunk.chunk_type == "method" and "UserService.authenticate" in chunk.name
        for chunk in chunks
    )
    assert any(
        chunk.chunk_type == "function" and chunk.name.startswith("build_service(")
        for chunk in chunks
    )
    authenticate = next(chunk for chunk in chunks if "UserService.authenticate" in chunk.name)
    assert authenticate.start_line > 0
    assert "lookup_user" in authenticate.content


def test_non_python_chunker_splits_large_files_by_line(tmp_path: Path):
    source = tmp_path / "notes.md"
    source.write_text(
        "\n".join(f"line {index}: {'x' * 80}" for index in range(80)), encoding="utf-8"
    )

    chunks = CodeChunker().chunk_file(source, display_path="notes.md")

    assert len(chunks) > 1
    assert all(chunk.chunk_type == "file" for chunk in chunks)
    assert chunks[0].end_line < chunks[-1].start_line


def test_python_analyzer_extracts_imports_inheritance_contains_and_calls(tmp_path: Path):
    source = tmp_path / "service.py"
    source.write_text(SAMPLE_CODE, encoding="utf-8")

    relations = CodeAnalyzer().analyze_file(source, display_path="service.py")

    assert CodeRelation("service.py", "file", None, "project.helpers", "imports") in relations
    assert CodeRelation("service.py", "UserService", None, "BaseService", "extends") in relations
    assert (
        CodeRelation(
            "service.py",
            "UserService",
            "service.py",
            "UserService.authenticate",
            "contains",
        )
        in relations
    )
    assert any(
        relation.from_name == "UserService.authenticate"
        and relation.relation_type == "calls"
        and relation.to_name == "self.lookup_user"
        for relation in relations
    )


def test_embedding_client_supports_empty_and_local_vectors():
    client = EmbeddingClient(provider="local")

    assert client.embed("") == []
    first = client.embed("Agent run tool call")
    second = client.embed("Agent run tool call")
    assert len(first) == 256
    assert first == second


def test_embedding_client_supports_ollama_and_openai_compatible_payloads(monkeypatch):
    calls: list[tuple[str, dict[str, object], bool]] = []

    ollama = EmbeddingClient(
        provider="ollama",
        model="nomic-embed-text",
        base_url="http://ollama.test",
    )
    monkeypatch.setattr(
        ollama,
        "_post_json",
        lambda url, payload, use_auth: (
            calls.append((url, payload, use_auth)) or {"embedding": [0.1, 0.2]}
        ),
    )
    assert ollama.embed("hello") == [0.1, 0.2]

    compatible = EmbeddingClient(
        provider="glm",
        model="embedding-3",
        base_url="https://embedding.test/v1",
        api_key="test-key",
    )
    monkeypatch.setattr(
        compatible,
        "_post_json",
        lambda url, payload, use_auth: (
            calls.append((url, payload, use_auth)) or {"data": [{"embedding": [0.3, 0.4]}]}
        ),
    )
    assert compatible.embed("world") == [0.3, 0.4]

    assert calls == [
        (
            "http://ollama.test/api/embeddings",
            {"model": "nomic-embed-text", "prompt": "hello"},
            False,
        ),
        (
            "https://embedding.test/v1/embeddings",
            {"model": "embedding-3", "input": "world"},
            True,
        ),
    ]


def test_query_tokenizer_keeps_symbols_and_discards_stopwords():
    tokens = tokenize_query("Agent 的 run 方法是怎么实现的")

    assert "Agent" in tokens
    assert "run" in tokens
    assert "怎么" not in tokens
    assert "实现" not in tokens


def test_sqlite_vector_store_search_keyword_relations_and_stats(tmp_path: Path):
    project = tmp_path / "project"
    project.mkdir()
    chunk_a = CodeChunk.class_chunk("service.py", "UserService", "class UserService", 1, 4)
    chunk_b = CodeChunk.method_chunk(
        "service.py",
        "UserService.authenticate(username)",
        "def authenticate(username): return True",
        5,
        6,
    )
    relation = CodeRelation(
        "service.py",
        "UserService",
        "service.py",
        "UserService.authenticate",
        "contains",
    )

    with VectorStore(project, storage_dir=tmp_path / "rag") as store:
        store.clear_project()
        store.insert_chunks([(chunk_a, [1.0, 0.0]), (chunk_b, [0.0, 1.0])])
        store.insert_relations([relation])

        semantic = store.search([1.0, 0.0], 2)
        keyword = store.search_by_keyword("authenticate")
        graph = store.get_relations("UserService")
        stats = store.get_stats()

    assert semantic[0].name == "UserService"
    assert semantic[0].similarity > 0.99
    assert keyword[0].start_line == 5
    assert graph == [relation]
    assert stats.chunk_count == 2
    assert stats.relation_count == 1


def test_hybrid_search_boosts_symbol_match_and_limits_same_file(tmp_path: Path):
    project = tmp_path / "project"
    project.mkdir()
    storage = tmp_path / "rag"
    getter = CodeChunk.method_chunk(
        "task.py",
        "Task.get_id(self)",
        "def get_id(self): return self.id",
        1,
        1,
    )
    agent = CodeChunk.method_chunk(
        "agent.py",
        "Agent.run(self, user_input)",
        "def run(self, user_input): execute the ReAct tool loop",
        10,
        20,
    )
    with VectorStore(project, storage_dir=storage) as store:
        store.replace_project(
            [(getter, [1.0, 0.0]), (agent, [0.8, 0.2])],
            [],
        )

    with CodeRetriever(
        project,
        embedding_client=StubEmbeddingClient([1.0, 0.0]),
        storage_dir=storage,
    ) as retriever:
        results = retriever.hybrid_search("Agent 的 ReAct run 是怎么实现的", 5)

    assert results[0].name.startswith("Agent.run")
    assert len([result for result in results if result.file_path == "agent.py"]) <= 2


def test_hybrid_search_does_not_boost_unrelated_top_level_functions_over_docs(
    tmp_path: Path,
):
    project = tmp_path / "project"
    project.mkdir()
    storage = tmp_path / "rag"
    document = CodeChunk.file_chunk(
        "docs/starport.md",
        "哪个组件负责管理充电优先级？AuroraChargeManager 负责管理充电优先级。",
        1,
        1,
    )
    unrelated_test = CodeChunk.function_chunk(
        "tests/test_memory.py",
        "test_clear_short_term_memory(tmp_path)",
        "def test_clear_short_term_memory(tmp_path): pass",
        10,
        10,
    )
    with VectorStore(project, storage_dir=storage) as store:
        store.replace_project(
            [
                (document, [0.678, 0.735]),
                (unrelated_test, [0.647, 0.762]),
            ],
            [],
        )

    with CodeRetriever(
        project,
        embedding_client=StubEmbeddingClient([1.0, 0.0]),
        storage_dir=storage,
    ) as retriever:
        results = retriever.hybrid_search("哪个组件负责管理充电优先级", 2)

    assert results[0].file_path == "docs/starport.md"


def test_code_index_service_and_search_code_tool_work_end_to_end(tmp_path: Path):
    project = tmp_path / "project"
    project.mkdir()
    (project / "service.py").write_text(SAMPLE_CODE, encoding="utf-8")
    ignored = project / ".venv"
    ignored.mkdir()
    (ignored / "ignored.py").write_text("def hidden(): pass", encoding="utf-8")
    storage = tmp_path / "rag"
    progress: list[str] = []
    service = RagService(
        project,
        storage_dir=storage,
        embedding_client=EmbeddingClient(provider="local"),
    )

    index_result = service.index(progress_callback=progress.append)
    search_results = service.search("UserService authenticate 用户登录", top_k=5)
    graph = service.graph("UserService")
    registry = build_default_registry(project, rag_service=service)
    tool_result = registry.execute(
        "search_code",
        {"query": "authenticate 用户", "top_k": 3},
    )

    assert index_result.file_count == 1
    assert index_result.chunk_count >= 4
    assert any(message.startswith("Starting code index") for message in progress)
    assert search_results
    assert search_results[0].file_path == "service.py"
    assert any(relation.relation_type == "contains" for relation in graph)
    assert "service.py" in tool_result
    assert "def authenticate" in tool_result


def test_code_index_accepts_paths_outside_project_root(tmp_path: Path):
    project = tmp_path / "project"
    outside = tmp_path / "outside"
    project.mkdir()
    outside.mkdir()
    (outside / "external.py").write_text("def external(): return True", encoding="utf-8")
    result = CodeIndex(
        project,
        embedding_client=StubEmbeddingClient(),
        storage_dir=tmp_path / "rag",
    ).index(outside)

    assert result.chunk_count >= 1
    assert result.error_count == 0


def test_rag_service_can_switch_to_an_external_index_project(tmp_path: Path):
    workspace = tmp_path / "workspace"
    external = tmp_path / "external"
    workspace.mkdir()
    external.mkdir()
    (external / "service.py").write_text(SAMPLE_CODE, encoding="utf-8")
    service = RagService(
        workspace,
        storage_dir=tmp_path / "rag",
        embedding_client=EmbeddingClient(provider="local"),
    )

    result = service.index(external)
    matches = service.search("UserService authenticate")

    assert result.chunk_count >= 4
    assert service.project_path == external.resolve()
    assert matches[0].file_path == "service.py"


def test_desktop_rag_indexes_multiple_sources_in_workspace_namespace(tmp_path: Path):
    workspace = tmp_path / "workspace"
    external = tmp_path / "external"
    workspace.mkdir()
    external.mkdir()
    local_file = workspace / "local.py"
    external_file = external / "external.py"
    local_file.write_text("def local_symbol(): return 'local'", encoding="utf-8")
    external_file.write_text("def external_symbol(): return 'external'", encoding="utf-8")
    service = RagService(
        workspace,
        storage_dir=tmp_path / "rag",
        embedding_client=EmbeddingClient(provider="local"),
    )

    result = service.index_sources([local_file, external_file, workspace])

    assert result.file_count == 2
    assert service.project_path == workspace.resolve()
    assert service.stats().file_count == 2
    assert service.search("local_symbol", top_k=3)
    assert any("external.py" in match.file_path for match in service.search("external_symbol", 3))


def test_rag_source_store_persists_sources_and_invalidates_stale_index_metadata(tmp_path: Path):
    first = tmp_path / "first.py"
    second = tmp_path / "second"
    first.write_text("print('first')", encoding="utf-8")
    second.mkdir()
    store = RagSourceStore(tmp_path / "project-data" / "rag" / "sources.json")

    store.add([first])
    store.record_index(
        {"chunk_count": 1},
        embedding_provider="local",
        embedding_model="hash-embedding-256",
    )
    assert store.snapshot()["last_indexed_at"]

    sources = store.add([first, second])
    snapshot = store.snapshot()

    assert len(sources) == 2
    assert snapshot["last_indexed_at"] is None
    assert RagSourceStore(store.path).snapshot()["sources"] == sources
    assert len(store.remove(first)) == 1


def test_search_code_schema_is_a_static_single_tool_contract(tmp_path: Path):
    registry = build_default_registry(
        tmp_path,
        rag_service=RagService(
            tmp_path,
            storage_dir=tmp_path / "rag",
            embedding_client=EmbeddingClient(provider="local"),
        ),
    )

    search_schema = next(
        schema["function"]
        for schema in registry.schemas()
        if schema["function"]["name"] == "search_code"
    )

    assert "semantic code index" in search_schema["description"]
    assert "Automatic retrieval" not in search_schema["description"]
    assert "explicitly asks" not in search_schema["description"]
    assert "glob_files" not in search_schema["description"]
    assert "grep_code" not in search_schema["description"]

    service = RagService(
        tmp_path,
        storage_dir=tmp_path / "rag",
        embedding_client=EmbeddingClient(provider="local"),
    )
    service.set_readiness_check(lambda: "Rebuild the desktop RAG index first.")
    guarded_registry = build_default_registry(tmp_path, rag_service=service)
    assert guarded_registry.execute("search_code", {"query": "demo"}) == (
        "Rebuild the desktop RAG index first."
    )


def test_formatter_includes_location_summary_and_real_code():
    result = CodeChunk.method_chunk(
        "agent.py",
        "Agent.run(self)",
        "def run(self):\n    return 'done'",
        10,
        11,
    )
    from stellarcode.rag.model import SearchResult

    output = SearchResultFormatter.format_for_tool(
        "Agent run",
        [
            SearchResult(
                result.file_path,
                result.chunk_type,
                result.name,
                result.content,
                result.start_line,
                result.end_line,
                0.95,
            )
        ],
    )

    assert "Most relevant entry" in output
    assert "agent.py:10-11" in output
    assert "10 | def run" in output
