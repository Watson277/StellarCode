from __future__ import annotations

import ast
from pathlib import Path

from stellarcode.rag.model import CodeChunk


class CodeChunker:
    MAX_CHUNK_CHARS = 2000

    def chunk_file(
        self,
        file_path: str | Path,
        display_path: str | None = None,
    ) -> list[CodeChunk]:
        path = Path(file_path)
        content = path.read_text(encoding="utf-8", errors="replace")
        chunk_path = display_path or str(path)
        if path.suffix.lower() != ".py":
            return self._chunk_large_text(chunk_path, content)
        return self._chunk_python_file(chunk_path, content)

    def _chunk_python_file(self, file_path: str, content: str) -> list[CodeChunk]:
        try:
            tree = ast.parse(content)
        except SyntaxError:
            return self._chunk_large_text(file_path, content)

        lines = content.splitlines()
        chunks: list[CodeChunk] = []

        for node in ast.walk(tree):
            if not isinstance(node, ast.ClassDef):
                continue
            start = _node_start_line(node)
            end = _node_end_line(node)
            header_end = min(start + 4, end)
            chunks.append(
                CodeChunk.class_chunk(
                    file_path,
                    node.name,
                    _extract_lines(lines, start, header_end),
                    start,
                    end,
                )
            )
            for member in node.body:
                if isinstance(member, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    method_start = _node_start_line(member)
                    method_end = _node_end_line(member)
                    chunks.append(
                        CodeChunk.method_chunk(
                            file_path,
                            f"{node.name}.{_function_signature(member)}",
                            _extract_lines(lines, method_start, method_end),
                            method_start,
                            method_end,
                        )
                    )

        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                start = _node_start_line(node)
                end = _node_end_line(node)
                chunks.append(
                    CodeChunk.function_chunk(
                        file_path,
                        _function_signature(node),
                        _extract_lines(lines, start, end),
                        start,
                        end,
                    )
                )

        if not chunks:
            return self._chunk_large_text(file_path, content)
        return sorted(chunks, key=lambda chunk: (chunk.start_line, chunk.chunk_type, chunk.name))

    def _chunk_large_text(self, file_path: str, content: str) -> list[CodeChunk]:
        lines = content.splitlines()
        if len(content) <= self.MAX_CHUNK_CHARS:
            return [
                CodeChunk.file_chunk(
                    file_path,
                    content,
                    start_line=1 if lines else 0,
                    end_line=len(lines),
                )
            ]

        chunks: list[CodeChunk] = []
        segment: list[str] = []
        segment_chars = 0
        segment_index = 1
        start_line = 1

        for line_number, line in enumerate(lines, start=1):
            added_chars = len(line) + 1
            if segment and segment_chars + added_chars > self.MAX_CHUNK_CHARS:
                chunks.append(
                    CodeChunk.file_chunk(
                        file_path,
                        "\n".join(segment).strip(),
                        start_line=start_line,
                        end_line=line_number - 1,
                        name=f"{file_path}#{segment_index}",
                    )
                )
                segment = []
                segment_chars = 0
                segment_index += 1
                start_line = line_number
            segment.append(line)
            segment_chars += added_chars

        if segment:
            chunks.append(
                CodeChunk.file_chunk(
                    file_path,
                    "\n".join(segment).strip(),
                    start_line=start_line,
                    end_line=len(lines),
                    name=f"{file_path}#{segment_index}",
                )
            )
        return chunks


def _node_start_line(node: ast.AST) -> int:
    decorators = getattr(node, "decorator_list", [])
    decorator_lines = [item.lineno for item in decorators if hasattr(item, "lineno")]
    return min([getattr(node, "lineno", 1), *decorator_lines])


def _node_end_line(node: ast.AST) -> int:
    return int(getattr(node, "end_lineno", getattr(node, "lineno", 1)))


def _extract_lines(lines: list[str], start_line: int, end_line: int) -> str:
    return "\n".join(lines[max(start_line - 1, 0) : end_line]).strip()


def _function_signature(node: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    prefix = "async " if isinstance(node, ast.AsyncFunctionDef) else ""
    try:
        arguments = ast.unparse(node.args)
    except Exception:
        arguments = "..."
    return f"{prefix}{node.name}({arguments})"
