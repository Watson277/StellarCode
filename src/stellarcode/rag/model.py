from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class CodeChunk:
    file_path: str
    chunk_type: str
    name: str
    content: str
    start_line: int = 0
    end_line: int = 0

    @classmethod
    def file_chunk(
        cls,
        file_path: str,
        content: str,
        start_line: int = 0,
        end_line: int = 0,
        name: str | None = None,
    ) -> "CodeChunk":
        return cls(
            file_path=file_path,
            chunk_type="file",
            name=name or file_path,
            content=content,
            start_line=start_line,
            end_line=end_line,
        )

    @classmethod
    def class_chunk(
        cls,
        file_path: str,
        class_name: str,
        content: str,
        start_line: int,
        end_line: int,
    ) -> "CodeChunk":
        return cls(file_path, "class", class_name, content, start_line, end_line)

    @classmethod
    def method_chunk(
        cls,
        file_path: str,
        method_name: str,
        content: str,
        start_line: int,
        end_line: int,
    ) -> "CodeChunk":
        return cls(file_path, "method", method_name, content, start_line, end_line)

    @classmethod
    def function_chunk(
        cls,
        file_path: str,
        function_name: str,
        content: str,
        start_line: int,
        end_line: int,
    ) -> "CodeChunk":
        return cls(file_path, "function", function_name, content, start_line, end_line)

    def to_embedding_text(self) -> str:
        return f"[{self.chunk_type}:{self.name}] {self.content}"


@dataclass(frozen=True)
class CodeRelation:
    from_file: str
    from_name: str
    to_file: str | None
    to_name: str
    relation_type: str


@dataclass(frozen=True)
class SearchResult:
    file_path: str
    chunk_type: str
    name: str
    content: str
    start_line: int
    end_line: int
    similarity: float

    @property
    def identity(self) -> str:
        return f"{self.file_path}#{self.chunk_type}#{self.name}#{self.start_line}"


@dataclass(frozen=True)
class IndexStats:
    chunk_count: int
    relation_count: int
    file_count: int = 0


@dataclass(frozen=True)
class IndexResult:
    chunk_count: int
    relation_count: int
    file_count: int
    error_count: int
    message: str
