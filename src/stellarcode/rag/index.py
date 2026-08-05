from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from stellarcode.rag.analyzer import CodeAnalyzer
from stellarcode.rag.chunker import CodeChunker
from stellarcode.rag.embedding import EmbeddingClient
from stellarcode.rag.model import CodeChunk, CodeRelation, IndexResult
from stellarcode.rag.store import VectorStore


INDEXED_SUFFIXES = {
    ".c",
    ".cpp",
    ".go",
    ".gradle",
    ".h",
    ".html",
    ".java",
    ".js",
    ".json",
    ".kt",
    ".md",
    ".properties",
    ".py",
    ".rs",
    ".sh",
    ".ts",
    ".tsx",
    ".xml",
    ".yaml",
    ".yml",
}
SKIPPED_DIRECTORIES = {
    ".git",
    ".idea",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".venv",
    ".vscode",
    "__pycache__",
    "build",
    "dist",
    "node_modules",
    "out",
    "target",
}


class CodeIndex:
    def __init__(
        self,
        project_path: str | Path,
        embedding_client: EmbeddingClient | None = None,
        storage_dir: str | Path | None = None,
        progress_callback: Callable[[str], None] | None = None,
    ) -> None:
        self.project_path = Path(project_path).resolve()
        self.embedding_client = embedding_client or EmbeddingClient()
        self.storage_dir = storage_dir
        self.progress_callback = progress_callback or (lambda _message: None)
        self.chunker = CodeChunker()
        self.analyzer = CodeAnalyzer()

    def index(self, index_path: str | Path | None = None) -> IndexResult:
        target = self._resolve_target(index_path)
        if not target.exists():
            message = f"Path does not exist: {target}"
            self._emit(f"ERROR: {message}")
            return IndexResult(0, 0, 0, 1, message)

        self._emit(f"Starting code index: {target}")
        files = self._collect_files(target)
        self._emit(f"Discovered {len(files)} file(s) to index")
        entries: list[tuple[CodeChunk, list[float]]] = []
        relations: list[CodeRelation] = []
        error_count = 0

        for index, file_path in enumerate(files, start=1):
            if index % 10 == 0 or index == len(files):
                self._emit(f"Progress: {index}/{len(files)} ({file_path.name})")
            display_path = self._display_path(file_path)
            try:
                chunks = self.chunker.chunk_file(file_path, display_path=display_path)
                embedded_chunks = [
                    (chunk, self.embedding_client.embed(chunk.to_embedding_text()))
                    for chunk in chunks
                ]
                if any(not embedding for _, embedding in embedded_chunks):
                    raise RuntimeError("embedding provider returned an empty vector")
                entries.extend(embedded_chunks)
            except Exception as exc:
                error_count += 1
                self._emit(f"WARNING: failed to index {display_path}: {exc}")
                continue

            if file_path.suffix.lower() == ".py":
                try:
                    relations.extend(
                        self.analyzer.analyze_file(file_path, display_path=display_path)
                    )
                except Exception as exc:
                    error_count += 1
                    self._emit(f"WARNING: failed to analyze {display_path}: {exc}")

        try:
            with VectorStore(self.project_path, storage_dir=self.storage_dir) as store:
                store.replace_project(entries, relations)
                stats = store.get_stats()
        except Exception as exc:
            message = f"Failed to persist code index: {exc}"
            self._emit(f"ERROR: {message}")
            return IndexResult(0, 0, len(files), error_count + 1, message)

        message = (
            f"Index complete: {stats.chunk_count} code chunk(s), "
            f"{stats.relation_count} relation(s), {error_count} error(s)"
        )
        self._emit(message)
        return IndexResult(
            stats.chunk_count,
            stats.relation_count,
            len(files),
            error_count,
            message,
        )

    def _resolve_target(self, index_path: str | Path | None) -> Path:
        if index_path is None or str(index_path).strip() in {"", "."}:
            return self.project_path
        raw = Path(index_path)
        target = raw.resolve() if raw.is_absolute() else (self.project_path / raw).resolve()
        return target

    def _display_path(self, path: Path) -> str:
        try:
            return path.relative_to(self.project_path).as_posix()
        except ValueError:
            return path.as_posix()

    def _collect_files(self, target: Path) -> list[Path]:
        if target.is_file():
            return [target] if target.suffix.lower() in INDEXED_SUFFIXES else []
        files = []
        for path in target.rglob("*"):
            if not path.is_file() or path.suffix.lower() not in INDEXED_SUFFIXES:
                continue
            try:
                relative_parts = path.relative_to(self.project_path).parts[:-1]
            except ValueError:
                relative_parts = path.parts[:-1]
            if any(part in SKIPPED_DIRECTORIES or part.startswith(".") for part in relative_parts):
                continue
            files.append(path)
        return sorted(files)

    def _emit(self, message: str) -> None:
        self.progress_callback(message)
