from __future__ import annotations

import os
import subprocess
from pathlib import Path

from stellarcode.hitl.handler import HitlHandler
from stellarcode.hitl.registry import HitlToolRegistry
from stellarcode.rag import RagService, SearchResultFormatter
from stellarcode.tools.registry import ToolDefinition, ToolExecutionError, ToolRegistry
from stellarcode.tools.registry import ToolOutput
from stellarcode.trace import TraceRecorder
from stellarcode.web import (
    SearchError,
    SearchProvider,
    SearchProviderFactory,
    WebFetchError,
    WebFetcher,
    assess_search_results,
    format_search_results,
)


MAX_COMMAND_OUTPUT_CHARS = 8000


def build_default_registry(
    workspace: str | Path | None = None,
    rag_service: RagService | None = None,
    hitl_handler: HitlHandler | None = None,
    max_parallel_tools: int = 4,
    tool_batch_timeout_seconds: float = 90,
    search_provider: SearchProvider | None = None,
    web_fetcher: WebFetcher | None = None,
    trace_recorder: TraceRecorder | None = None,
) -> ToolRegistry:
    root = Path(workspace or Path.cwd()).resolve()
    code_search = rag_service or RagService(root)
    active_search_provider = search_provider or SearchProviderFactory.create_smart()
    active_web_fetcher = web_fetcher or WebFetcher()
    registry = ToolRegistry(
        max_parallel_tools=max_parallel_tools,
        batch_timeout_seconds=tool_batch_timeout_seconds,
        trace_recorder=trace_recorder,
    )

    registry.register(
        ToolDefinition(
            name="read_file",
            description=(
                "Read a UTF-8 text file. Absolute paths and paths outside the working "
                "directory are supported."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Absolute path or path relative to the working directory.",
                    },
                    "max_chars": {
                        "type": "integer",
                        "description": "Maximum characters to return.",
                        "default": 20000,
                    },
                },
                "required": ["path"],
                "additionalProperties": False,
            },
            handler=lambda path, max_chars=20000: _read_file(root, path, max_chars),
        )
    )

    registry.register(
        ToolDefinition(
            name="write_file",
            description=(
                "Write UTF-8 text to a file. Absolute paths and paths outside the working "
                "directory are supported."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Absolute path or path relative to the working directory.",
                    },
                    "content": {"type": "string", "description": "Full file content to write."},
                },
                "required": ["path", "content"],
                "additionalProperties": False,
            },
            handler=lambda path, content: _write_file(root, path, content),
        )
    )

    registry.register(
        ToolDefinition(
            name="delete_file",
            description=(
                "Delete one file at any accessible path. This does not delete "
                "directories and should be used instead of a shell command for file deletion."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": (
                            "Absolute file path or path relative to the working directory."
                        ),
                    },
                },
                "required": ["path"],
                "additionalProperties": False,
            },
            handler=lambda path: _delete_file(root, path),
        )
    )

    registry.register(
        ToolDefinition(
            name="list_dir",
            description="List files and directories at any accessible location.",
            parameters={
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": (
                            "Absolute directory path or path relative to the working directory."
                        ),
                        "default": ".",
                    },
                    "max_entries": {
                        "type": "integer",
                        "description": "Maximum entries to return (1-1000).",
                        "default": 200,
                        "minimum": 1,
                        "maximum": 1000,
                    },
                },
                "additionalProperties": False,
            },
            handler=lambda path=".", max_entries=200: _list_dir(root, path, max_entries),
        )
    )

    registry.register(
        ToolDefinition(
            name="execute_command",
            description=(
                "Run a command in the current working directory and return stdout/stderr. "
                "A string runs through the platform shell; an argv array runs directly."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "command": {
                        "oneOf": [
                            {"type": "string"},
                            {"type": "array", "items": {"type": "string"}},
                        ],
                        "description": "Command to run. Prefer an argv array.",
                    },
                    "timeout_seconds": {
                        "type": "integer",
                        "description": "Maximum runtime in seconds.",
                        "default": 30,
                    },
                },
                "required": ["command"],
                "additionalProperties": False,
            },
            handler=lambda command, timeout_seconds=30: _execute_command(
                root, command, timeout_seconds
            ),
        )
    )

    registry.register(
        ToolDefinition(
            name="web_search",
            description=(
                "Search the public web for current or recently changed information and "
                "return ranked titles, URLs, dates, snippets, and a relevance assessment. "
                "The search router may try a fallback source when the primary provider is "
                "off-topic. For factual verification, use web_fetch on at most three URLs "
                "listed under Fetch guidance."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Concise web search query.",
                    },
                    "top_k": {
                        "type": "integer",
                        "description": "Maximum search results to return (1-20).",
                        "default": 5,
                        "minimum": 1,
                        "maximum": 20,
                    },
                },
                "required": ["query"],
                "additionalProperties": False,
            },
            handler=lambda query, top_k=5: _web_search(
                active_search_provider, query, top_k
            ),
        )
    )

    registry.register(
        ToolDefinition(
            name="web_fetch",
            description=(
                "Fetch a known public HTTP or HTTPS URL and return readable page content. "
                "HTML navigation and page chrome are removed. Local/private network targets "
                "are blocked, redirects are rechecked, and JavaScript-only pages may be empty."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "HTTP or HTTPS URL to fetch."},
                    "max_chars": {
                        "type": "integer",
                        "description": "Maximum response characters to return (1-100000).",
                        "default": 8000,
                        "minimum": 1,
                        "maximum": 100000,
                    },
                    "timeout_seconds": {
                        "type": "integer",
                        "description": "Maximum request time in seconds (1-120).",
                        "default": 30,
                        "minimum": 1,
                        "maximum": 120,
                    },
                },
                "required": ["url"],
                "additionalProperties": False,
            },
            handler=lambda url, max_chars=8000, timeout_seconds=30: _web_fetch(
                active_web_fetcher, url, max_chars, timeout_seconds
            ),
        )
    )

    registry.register(
        ToolDefinition(
            name="search_code",
            description=(
                "Search the indexed codebase by natural language. Returns relevant real code "
                "chunks with file paths and line numbers. Run /index in the CLI first."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Natural-language code question or symbol description.",
                    },
                    "top_k": {
                        "type": "integer",
                        "description": "Maximum code chunks to return (1-20).",
                        "default": 5,
                        "minimum": 1,
                        "maximum": 20,
                    },
                },
                "required": ["query"],
                "additionalProperties": False,
            },
            handler=lambda query, top_k=5: _search_code(code_search, query, top_k),
        )
    )

    if hitl_handler is not None:
        return HitlToolRegistry(registry, hitl_handler)
    return registry


def _resolve_path(root: Path, user_path: str) -> Path:
    raw_path = Path(user_path).expanduser()
    return raw_path.resolve() if raw_path.is_absolute() else (root / raw_path).resolve()


def _display_path(root: Path, path: Path) -> str:
    try:
        return str(path.relative_to(root))
    except ValueError:
        return str(path)


def _read_file(root: Path, path: str, max_chars: int = 20000) -> str:
    target = _resolve_path(root, path)
    if not target.is_file():
        raise ToolExecutionError(f"File not found: {path}")
    content = target.read_text(encoding="utf-8", errors="replace")
    if len(content) > max_chars:
        return content[:max_chars] + f"\n...[truncated at {max_chars} chars]"
    return content


def _write_file(root: Path, path: str, content: str) -> str:
    target = _resolve_path(root, path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    return f"Wrote {len(content)} characters to {_display_path(root, target)}"


def _delete_file(root: Path, path: str) -> str:
    target = _safe_delete_path(root, path)
    display_path = _display_path(root, target)
    if target.is_symlink():
        target.unlink()
    elif not target.exists():
        raise ToolExecutionError(f"File not found: {path}")
    elif not target.is_file():
        raise ToolExecutionError(f"Path is not a file: {path}")
    else:
        target.unlink()
    return f"Deleted file: {display_path}"


def _safe_delete_path(root: Path, user_path: str) -> Path:
    raw_path = Path(user_path).expanduser()
    candidate = raw_path if raw_path.is_absolute() else root / raw_path
    return Path(os.path.abspath(candidate))


def _list_dir(root: Path, path: str = ".", max_entries: int = 200) -> str:
    target = _resolve_path(root, path)
    if not target.is_dir():
        raise ToolExecutionError(f"Directory not found: {path}")
    limit = max(1, min(int(max_entries), 1000))
    entries = sorted(target.iterdir(), key=lambda item: (not item.is_dir(), item.name.lower()))
    lines = []
    for entry in entries[:limit]:
        kind = "dir" if entry.is_dir() else "file"
        lines.append(f"[{kind}] {_display_path(root, entry)}")
    if len(entries) > limit:
        lines.append(f"...[truncated: showing {limit} of {len(entries)} entries]")
    return "\n".join(lines) if lines else "(empty directory)"


def _execute_command(
    root: Path,
    command: str | list[str],
    timeout_seconds: int = 30,
) -> ToolOutput:
    argv = _normalize_command(command)
    if not argv:
        raise ToolExecutionError("Command cannot be empty.")

    try:
        completed = subprocess.run(
            argv,
            cwd=root,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            shell=False,
            encoding="utf-8",
            errors="replace",
        )
    except FileNotFoundError as exc:
        raise ToolExecutionError(f"Command not found: {argv[0]}") from exc
    except subprocess.TimeoutExpired as exc:
        raise ToolExecutionError(f"Command timed out after {timeout_seconds}s.") from exc

    output = []
    output.append(f"exit_code: {completed.returncode}")
    if completed.stdout:
        output.append("stdout:")
        output.append(completed.stdout.strip())
    if completed.stderr:
        output.append("stderr:")
        output.append(completed.stderr.strip())
    full_output = "\n".join(output)
    return ToolOutput(
        text=_truncate_command_output(full_output),
        trace_text=full_output,
    )


def _normalize_command(command: str | list[str]) -> list[str]:
    if isinstance(command, list):
        if not all(isinstance(part, str) for part in command):
            raise ToolExecutionError("Command array must contain only strings.")
        return command
    if not isinstance(command, str):
        raise ToolExecutionError("Command must be a string or string array.")
    command = command.strip()
    if not command:
        return []
    if os.name == "nt":
        return ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command]
    return ["/bin/sh", "-lc", command]


def _truncate_command_output(output: str) -> str:
    if len(output) <= MAX_COMMAND_OUTPUT_CHARS:
        return output
    marker = (
        f"\n...[command output truncated; original length: {len(output)} characters]"
    )
    preview_length = max(0, MAX_COMMAND_OUTPUT_CHARS - len(marker))
    return f"{output[:preview_length]}{marker}"


def _web_search(
    provider: SearchProvider,
    query: str,
    top_k: int = 5,
) -> str:
    if not provider.is_ready():
        raise ToolExecutionError(
            f"Web search unavailable ({provider.name}): {provider.unavailable_hint()}"
        )
    try:
        results = provider.search(query, max(1, min(int(top_k), 20)))
    except SearchError as exc:
        raise ToolExecutionError(f"Web search unavailable ({provider.name}): {exc}") from exc
    assessment = assess_search_results(query, results)
    return format_search_results(
        provider.name,
        query.strip(),
        results,
        quality=assessment.quality,
        core_terms=assessment.core_terms,
        relevant_count=assessment.relevant_count,
        fetch_candidates=assessment.fetch_candidates,
    )


def _web_fetch(
    fetcher: WebFetcher,
    url: str,
    max_chars: int = 8000,
    timeout_seconds: int = 30,
) -> str:
    try:
        return fetcher.fetch(url, max_chars, timeout_seconds)
    except WebFetchError as exc:
        raise ToolExecutionError(str(exc)) from exc


def _search_code(rag_service: RagService, query: str, top_k: int = 5) -> str:
    if not query.strip():
        raise ToolExecutionError("Code search query cannot be empty.")
    stats = rag_service.stats()
    if stats.chunk_count == 0:
        return "Code index is empty. Run /index in the CLI before using search_code."
    results = rag_service.search(query, max(1, min(int(top_k), 20)))
    formatted = SearchResultFormatter.format_for_tool(query, results)
    return f"Indexed project: {rag_service.project_path}\n\n{formatted}"
