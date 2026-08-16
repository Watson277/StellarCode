from __future__ import annotations

import os
import signal
import stat
import subprocess
import tempfile
import threading
import time
from functools import partial
from pathlib import Path

from stellarcode.hitl.handler import HitlHandler
from stellarcode.hitl.registry import HitlToolRegistry
from stellarcode.path_utils import subprocess_safe_path
from stellarcode.protection import preview_delete_file, preview_write_file
from stellarcode.rag import RagService, SearchResultFormatter
from stellarcode.tools.registry import (
    ToolDefinition,
    ToolExecutionError,
    ToolOutput,
    ToolRegistry,
    tool_cancellation_reason,
    tool_cancellation_requested,
)
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
_FILE_MUTATION_LOCK = threading.RLock()
_MISSING_FILE_GUARD = "__stellarcode_missing__"


def build_default_registry(
    workspace: str | Path | None = None,
    rag_service: RagService | None = None,
    hitl_handler: HitlHandler | None = None,
    max_parallel_tools: int = 4,
    tool_batch_timeout_seconds: float = 90,
    search_provider: SearchProvider | None = None,
    web_fetcher: WebFetcher | None = None,
    trace_recorder: TraceRecorder | None = None,
    rag_auto_retrieval: bool = True,
) -> ToolRegistry:
    root = subprocess_safe_path(workspace or Path.cwd())
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
            handler=partial(_write_file, root),
            previewer=lambda path, content: preview_write_file(root, path, content),
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
            handler=partial(_delete_file, root),
            previewer=lambda path: preview_delete_file(root, path),
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
                "chunks with file paths and line numbers. The index can be built from the "
                "desktop RAG settings or with /index in the CLI. "
                + (
                    "Automatic retrieval is enabled: use it before guessing about architecture, "
                    "behavior, or symbol locations, then read_file for exact context."
                    if rag_auto_retrieval
                    else "Automatic retrieval is disabled: call this tool only when the user "
                    "explicitly asks to use RAG or search the semantic code index."
                )
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


def _write_file(
    root: Path,
    path: str,
    content: str,
    *,
    __expected_path: str | None = None,
    __expected_before_sha256: str | None = None,
) -> str:
    target = _resolve_path(root, path)
    with _FILE_MUTATION_LOCK:
        if tool_cancellation_requested():
            raise ToolExecutionError("Task cancelled before file write.")
        preview = preview_write_file(root, path, content)
        _verify_change_guard(preview, __expected_path, __expected_before_sha256)
        if preview["operation"] == "no_change":
            return f"No changes needed for {_display_path(root, target)}"
        target.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{target.name}.",
            dir=target.parent,
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            if target.exists():
                temporary.chmod(stat.S_IMODE(target.stat().st_mode))
            os.replace(temporary, target)
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass
        return (
            f"Wrote {len(content)} characters to {_display_path(root, target)} "
            f"({preview['operation']}, +{preview['additions']} -{preview['deletions']})"
        )


def _delete_file(
    root: Path,
    path: str,
    *,
    __expected_path: str | None = None,
    __expected_before_sha256: str | None = None,
) -> str:
    with _FILE_MUTATION_LOCK:
        if tool_cancellation_requested():
            raise ToolExecutionError("Task cancelled before file deletion.")
        target = _safe_delete_path(root, path)
        preview = preview_delete_file(root, path)
        display_path = str(preview["path"])
        _verify_change_guard(preview, __expected_path, __expected_before_sha256)
        if target.is_symlink():
            target.unlink()
        elif not target.exists():
            raise ToolExecutionError(f"File not found: {path}")
        elif not target.is_file():
            raise ToolExecutionError(f"Path is not a file: {path}")
        else:
            target.unlink()
        return f"Deleted file: {display_path}"


def _verify_change_guard(
    preview: dict[str, object],
    expected_path: str | None,
    expected_before_sha256: str | None,
) -> None:
    if expected_path is None and expected_before_sha256 is None:
        return
    expected_hash = (
        None
        if expected_before_sha256 == _MISSING_FILE_GUARD
        else expected_before_sha256
    )
    if preview.get("path") == expected_path and preview.get("before_sha256") == expected_hash:
        return
    raise ToolExecutionError(
        "Modification guard stopped the operation because the target changed "
        "after its diff was approved. Read the file again and retry."
    )


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

    creation_flags = 0
    popen_options = {}
    if os.name == "nt":
        creation_flags = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        popen_options["start_new_session"] = True

    try:
        process = subprocess.Popen(
            argv,
            cwd=subprocess_safe_path(root),
            env=_tool_process_environment(),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            shell=False,
            encoding="utf-8",
            errors="replace",
            creationflags=creation_flags,
            **popen_options,
        )
    except FileNotFoundError as exc:
        raise ToolExecutionError(f"Command not found: {argv[0]}") from exc

    deadline = time.monotonic() + timeout_seconds
    while True:
        cancelled = tool_cancellation_requested()
        remaining = deadline - time.monotonic()
        if cancelled or remaining <= 0:
            _terminate_process_tree(process)
            stdout, stderr = _collect_terminated_process(process)
            detail = _partial_command_output(stdout, stderr)
            if cancelled:
                if tool_cancellation_reason() == "task":
                    message = "Command cancelled by the active task."
                else:
                    message = "Command cancelled because its parallel tool batch timed out."
            else:
                message = f"Command timed out after {timeout_seconds}s."
            if detail:
                message += f" Partial output:\n{detail}"
            raise ToolExecutionError(message)
        try:
            stdout, stderr = process.communicate(timeout=min(0.1, remaining))
            break
        except subprocess.TimeoutExpired:
            continue

    output = []
    output.append(f"exit_code: {process.returncode}")
    if stdout:
        output.append("stdout:")
        output.append(stdout.strip())
    if stderr:
        output.append("stderr:")
        output.append(stderr.strip())
    full_output = "\n".join(output)
    return ToolOutput(
        text=_truncate_command_output(full_output),
        trace_text=full_output,
    )


def _tool_process_environment() -> dict[str, str]:
    """Remove Runtime-only import paths before launching workspace commands."""

    environment = os.environ.copy()
    runtime_pythonpath = environment.pop("STELLARCODE_RUNTIME_PYTHONPATH", None)
    inherited_pythonpath = environment.pop("STELLARCODE_TOOL_PYTHONPATH", None)
    if runtime_pythonpath and environment.get("PYTHONPATH") == runtime_pythonpath:
        environment.pop("PYTHONPATH", None)
    if inherited_pythonpath:
        environment["PYTHONPATH"] = inherited_pythonpath
    return environment


def _terminate_process_tree(process: subprocess.Popen[str]) -> None:
    if process.poll() is not None:
        return
    if os.name == "nt":
        try:
            terminated = subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=5,
                check=False,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            if terminated.returncode == 0:
                return
        except (OSError, subprocess.SubprocessError):
            pass
    else:
        try:
            os.killpg(os.getpgid(process.pid), signal.SIGKILL)
            return
        except OSError:
            pass
    try:
        process.kill()
    except OSError:
        pass


def _collect_terminated_process(
    process: subprocess.Popen[str],
) -> tuple[str, str]:
    try:
        return process.communicate(timeout=5)
    except subprocess.TimeoutExpired:
        try:
            process.kill()
        except OSError:
            pass
        try:
            return process.communicate(timeout=1)
        except subprocess.TimeoutExpired:
            return "", ""


def _partial_command_output(stdout: str, stderr: str) -> str:
    values = []
    if stdout:
        values.append(f"stdout:\n{stdout.strip()}")
    if stderr:
        values.append(f"stderr:\n{stderr.strip()}")
    return _truncate_command_output("\n".join(values))


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
    unavailable_reason = rag_service.unavailable_reason()
    if unavailable_reason:
        return unavailable_reason
    stats = rag_service.stats()
    if stats.chunk_count == 0:
        return (
            "Code index is empty. Add files or folders and build the index in desktop "
            "Settings > RAG, or run /index in the CLI."
        )
    results = rag_service.search(query, max(1, min(int(top_k), 20)))
    formatted = SearchResultFormatter.format_for_tool(query, results)
    return f"Indexed project: {rag_service.project_path}\n\n{formatted}"
