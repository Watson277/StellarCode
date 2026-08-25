"""Formats retrieved code chunks into bounded, source-attributed prompt context."""

from __future__ import annotations

from pathlib import Path

from stellarcode.rag.model import CodeRelation, SearchResult
from stellarcode.rag.tokenizer import tokenize_query


class SearchResultFormatter:
    @staticmethod
    def format_for_cli(query: str, results: list[SearchResult]) -> str:
        lines = [f"Found {len(results)} related code chunk(s).", "", _build_summary(query, results)]
        for index, result in enumerate(results, start=1):
            lines.extend(
                [
                    "",
                    _result_heading(index, result),
                    _numbered_snippet(result, max_chars=320),
                ]
            )
        return "\n".join(lines).strip()

    @staticmethod
    def format_for_tool(query: str, results: list[SearchResult]) -> str:
        lines = ["Search summary:", _build_summary(query, results), "", "Code results:"]
        for index, result in enumerate(results, start=1):
            lines.extend(
                [
                    "",
                    _result_heading(index, result),
                    _numbered_snippet(result, max_chars=800),
                ]
            )
        return "\n".join(lines).strip()

    @staticmethod
    def format_graph(name: str, relations: list[CodeRelation]) -> str:
        if not relations:
            return f"No code relations found for: {name}"
        lines = [f"Code relations for {name}:", f"Found {len(relations)} relation(s).", ""]
        for relation in relations:
            target = relation.to_name
            target_file = f" ({relation.to_file})" if relation.to_file else ""
            lines.append(
                f"{relation.from_name} --{relation.relation_type}--> "
                f"{target}{target_file}"
            )
        return "\n".join(lines)


def _build_summary(query: str, results: list[SearchResult]) -> str:
    if not results:
        return "No indexed code chunk matched the query."
    top = results[0]
    files = list(dict.fromkeys(Path(result.file_path).name for result in results))
    tokens = tokenize_query(query)[:3]
    token_text = ", ".join(tokens) if tokens else "semantic similarity"
    file_text = ", ".join(files[:3])
    if len(files) > 3:
        file_text += ", ..."
    return "\n".join(
        [
            f"- Most relevant entry: [{top.chunk_type}:{top.name}] in {_location(top)}.",
            f"- Results are concentrated in: {file_text}.",
            f"- Ranking combined semantic similarity with: {token_text}.",
        ]
    )


def _result_heading(index: int, result: SearchResult) -> str:
    return (
        f"{index}. [{result.chunk_type}:{result.name}] "
        f"score={result.similarity:.3f} {_location(result)}"
    )


def _location(result: SearchResult) -> str:
    if result.start_line > 0:
        end_line = result.end_line or result.start_line
        return f"{result.file_path}:{result.start_line}-{end_line}"
    return result.file_path


def _numbered_snippet(result: SearchResult, max_chars: int) -> str:
    content = result.content.strip().replace("\r\n", "\n").replace("\r", "\n")
    if not content:
        return "   (empty code chunk)"
    if len(content) > max_chars:
        content = f"{content[:max_chars]}\n..."
    start_line = result.start_line if result.start_line > 0 else 1
    return "\n".join(
        f"   {start_line + offset:>4} | {line}"
        for offset, line in enumerate(content.splitlines())
    )
