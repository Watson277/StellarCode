"""Budgeted glob and text-search helpers used before semantic RAG retrieval."""

from __future__ import annotations

import fnmatch
import json
import os
import re
import shutil
import stat
import subprocess
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from stellarcode.tools.registry import ToolExecutionError, tool_cancellation_requested


SEARCH_EXCLUDED_DIRECTORIES = frozenset(
    {
        ".git",
        ".stellarcode",
        ".venv",
        "venv",
        "node_modules",
        "__pycache__",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        ".tox",
        ".nox",
        ".gradle",
        ".next",
        ".turbo",
        "target",
        "dist",
        "build",
    }
)
MAX_SCAN_FILES = 50_000
MAX_GREP_FILE_BYTES = 2 * 1024 * 1024
MAX_RENDERED_LINE_CHARS = 1_200


class CodeSearchError(ToolExecutionError):
    pass


@dataclass(frozen=True)
class _GrepMatch:
    path: Path
    line_number: int


def glob_files(
    workspace: Path,
    pattern: str,
    path: str = ".",
    max_results: int = 50,
) -> str:
    normalized_pattern = _normalized_pattern(pattern)
    base = _search_root(workspace, path)
    limit = max(1, min(int(max_results), 200))

    rg = shutil.which("rg")
    if rg:
        matches, partial = _glob_with_rg(base, normalized_pattern, limit, rg)
        engine = "ripgrep"
    else:
        matches, partial = _glob_with_python(base, normalized_pattern, limit)
        engine = "python"

    if not matches:
        return f"No files matched: {pattern} (engine={engine})"
    rendered = [
        f"Matched {len(matches)} file(s) (engine={engine}, partial={'true' if partial else 'false'}):"
    ]
    rendered.extend(
        f"{index}. {_display_path(workspace, target)}"
        for index, target in enumerate(matches, start=1)
    )
    if partial:
        rendered.append(
            "partial: true (result or scan limit reached; narrow path/pattern to continue)"
        )
    return "\n".join(rendered)


def grep_code(
    workspace: Path,
    pattern: str,
    path: str = ".",
    glob: str | None = None,
    regex: bool = False,
    case_sensitive: bool = True,
    context_lines: int = 0,
    max_results: int = 50,
    head_limit: int = 20,
    max_chars: int = 24_000,
) -> str:
    if not isinstance(pattern, str) or not pattern:
        raise CodeSearchError("pattern cannot be empty")
    base = _search_root(workspace, path)
    normalized_glob = _normalized_pattern(glob) if glob else None
    result_limit = max(1, min(int(max_results), 200))
    per_file_limit = max(1, min(int(head_limit), 50))
    context = max(0, min(int(context_lines), 5))
    character_limit = max(1_000, min(int(max_chars), 60_000))

    if regex:
        try:
            re.compile(pattern, 0 if case_sensitive else re.IGNORECASE)
        except re.error as exc:
            raise CodeSearchError(f"invalid regular expression: {exc}") from exc

    rg = shutil.which("rg")
    if rg:
        matches, partial = _grep_with_rg(
            base,
            pattern,
            normalized_glob,
            regex,
            case_sensitive,
            result_limit,
            per_file_limit,
            rg,
        )
        engine = "ripgrep"
    else:
        matches, partial = _grep_with_python(
            base,
            pattern,
            normalized_glob,
            regex,
            case_sensitive,
            result_limit,
            per_file_limit,
        )
        engine = "python"

    if not matches:
        return f"No matches found: {pattern} (engine={engine})"
    return _render_grep_matches(
        workspace,
        matches,
        pattern,
        engine,
        context,
        character_limit,
        partial,
    )


def _glob_with_rg(
    base: Path,
    pattern: str,
    limit: int,
    rg: str,
) -> tuple[list[Path], bool]:
    argv = [rg, "--files", "--hidden", "--color", "never", "--glob", pattern]
    argv.extend(_rg_exclusion_arguments())
    argv.append(".")
    process = _start_rg(argv, base)
    matches: list[Path] = []
    partial = False
    try:
        assert process.stdout is not None
        for raw_line in process.stdout:
            _raise_if_cancelled(process)
            value = raw_line.rstrip("\r\n")
            if not value:
                continue
            target = (base / value).resolve()
            if not target.is_file() or target.is_symlink():
                continue
            matches.append(target)
            if len(matches) >= limit:
                partial = True
                process.kill()
                break
        return_code = process.wait(timeout=5)
        error = _read_process_error(process)
        if return_code not in {0, 1} and not partial:
            raise CodeSearchError(error or f"ripgrep exited with code {return_code}")
    finally:
        _close_process(process)
    return sorted(matches, key=lambda item: str(item).lower()), partial


def _glob_with_python(
    base: Path,
    pattern: str,
    limit: int,
) -> tuple[list[Path], bool]:
    matches: list[Path] = []
    scanned = 0
    for target in _walk_files(base):
        scanned += 1
        relative = target.relative_to(base).as_posix()
        if _matches_glob(relative, target.name, pattern):
            matches.append(target)
            if len(matches) >= limit:
                return matches, True
        if scanned >= MAX_SCAN_FILES:
            return matches, True
    return matches, False


def _grep_with_rg(
    base: Path,
    pattern: str,
    glob_pattern: str | None,
    regex: bool,
    case_sensitive: bool,
    max_results: int,
    head_limit: int,
    rg: str,
) -> tuple[list[_GrepMatch], bool]:
    argv = [
        rg,
        "--json",
        "--line-number",
        "--hidden",
        "--color",
        "never",
        "--max-count",
        str(head_limit),
        "--max-filesize",
        str(MAX_GREP_FILE_BYTES),
    ]
    argv.append("--case-sensitive" if case_sensitive else "--ignore-case")
    if not regex:
        argv.append("--fixed-strings")
    if glob_pattern:
        argv.extend(["--glob", glob_pattern])
    argv.extend(_rg_exclusion_arguments())
    argv.extend(["--", pattern, "."])
    process = _start_rg(argv, base)
    matches: list[_GrepMatch] = []
    partial = False
    seen: set[tuple[str, int]] = set()
    try:
        assert process.stdout is not None
        for raw_line in process.stdout:
            _raise_if_cancelled(process)
            try:
                payload = json.loads(raw_line)
            except json.JSONDecodeError:
                continue
            if payload.get("type") != "match":
                continue
            data = payload.get("data") or {}
            path_data = data.get("path") or {}
            path_text = path_data.get("text")
            line_number = data.get("line_number")
            if not isinstance(path_text, str) or not isinstance(line_number, int):
                continue
            target = (base / path_text).resolve()
            if not target.is_file() or target.is_symlink():
                continue
            key = (str(target), line_number)
            if key in seen:
                continue
            seen.add(key)
            matches.append(_GrepMatch(target, line_number))
            if len(matches) >= max_results:
                partial = True
                process.kill()
                break
        return_code = process.wait(timeout=5)
        error = _read_process_error(process)
        if return_code not in {0, 1} and not partial:
            raise CodeSearchError(error or f"ripgrep exited with code {return_code}")
    finally:
        _close_process(process)
    matches.sort(key=lambda item: (str(item.path).lower(), item.line_number))
    return matches, partial


def _grep_with_python(
    base: Path,
    pattern: str,
    glob_pattern: str | None,
    regex: bool,
    case_sensitive: bool,
    max_results: int,
    head_limit: int,
) -> tuple[list[_GrepMatch], bool]:
    expression = (
        re.compile(pattern, 0 if case_sensitive else re.IGNORECASE)
        if regex
        else None
    )
    needle = pattern if case_sensitive else pattern.casefold()
    matches: list[_GrepMatch] = []
    scanned = 0
    for target in _walk_files(base):
        scanned += 1
        relative = target.relative_to(base).as_posix()
        if glob_pattern and not _matches_glob(relative, target.name, glob_pattern):
            continue
        try:
            if target.stat().st_size > MAX_GREP_FILE_BYTES:
                continue
            raw = target.read_bytes()
        except OSError:
            continue
        if b"\x00" in raw:
            continue
        text = raw.decode("utf-8", errors="replace")
        file_matches = 0
        for line_number, line in enumerate(text.splitlines(), start=1):
            found = bool(expression.search(line)) if expression else needle in (
                line if case_sensitive else line.casefold()
            )
            if not found:
                continue
            matches.append(_GrepMatch(target, line_number))
            file_matches += 1
            if len(matches) >= max_results:
                return matches, True
            if file_matches >= head_limit:
                break
        if scanned >= MAX_SCAN_FILES:
            return matches, True
    return matches, False


def _render_grep_matches(
    workspace: Path,
    matches: list[_GrepMatch],
    pattern: str,
    engine: str,
    context_lines: int,
    max_chars: int,
    partial: bool,
) -> str:
    header = (
        f"Matched {len(matches)} line(s) for {pattern!r} "
        f"(engine={engine}, partial={'true' if partial else 'false'}):\n"
    )
    rendered = header
    rendered_count = 0
    line_cache: dict[Path, list[str]] = {}
    for index, match in enumerate(matches, start=1):
        lines = line_cache.get(match.path)
        if lines is None:
            try:
                lines = match.path.read_text(encoding="utf-8", errors="replace").splitlines()
            except OSError:
                lines = []
            line_cache[match.path] = lines
        block = [f"{index}. {_display_path(workspace, match.path)}:{match.line_number}"]
        start = max(1, match.line_number - context_lines)
        end = min(len(lines), match.line_number + context_lines)
        for line_number in range(start, end + 1):
            marker = ">" if line_number == match.line_number else " "
            value = lines[line_number - 1]
            if len(value) > MAX_RENDERED_LINE_CHARS:
                value = value[:MAX_RENDERED_LINE_CHARS] + "...[line truncated]"
            block.append(f"   {marker}{line_number:5d} | {value}")
        candidate = "\n".join(block) + "\n"
        if len(rendered) + len(candidate) > max_chars:
            partial = True
            break
        rendered += candidate
        rendered_count += 1
    if partial or rendered_count < len(matches):
        rendered += (
            f"partial: true (rendered {rendered_count} result(s) within max_chars={max_chars}; "
            "narrow path/glob/pattern to continue)\n"
        )
    suggested: list[str] = []
    for match in matches[:rendered_count]:
        display = _display_path(workspace, match.path)
        if display not in suggested:
            suggested.append(display)
        if len(suggested) >= 3:
            break
    if suggested:
        rendered += "suggested_reads:\n" + "\n".join(
            f'- read_file {{"path":{json.dumps(path, ensure_ascii=False)}}}'
            for path in suggested
        )
    return rendered.rstrip()


def _walk_files(base: Path) -> Iterable[Path]:
    stack = [base]
    while stack:
        if tool_cancellation_requested():
            raise CodeSearchError("task cancelled during code search")
        directory = stack.pop()
        try:
            entries = sorted(os.scandir(directory), key=lambda item: item.name.lower())
        except OSError:
            continue
        child_directories: list[Path] = []
        for entry in entries:
            if entry.name.lower() in SEARCH_EXCLUDED_DIRECTORIES:
                continue
            try:
                status = entry.stat(follow_symlinks=False)
                if entry.is_symlink() or _is_reparse_point(status):
                    continue
                if entry.is_dir(follow_symlinks=False):
                    child_directories.append(Path(entry.path))
                elif entry.is_file(follow_symlinks=False):
                    yield Path(entry.path)
            except OSError:
                continue
        stack.extend(reversed(child_directories))


def _search_root(workspace: Path, path: str) -> Path:
    raw = Path(path or ".").expanduser()
    target = raw.resolve() if raw.is_absolute() else (workspace / raw).resolve()
    if not target.is_dir():
        raise CodeSearchError(f"search directory not found: {path}")
    return target


def _normalized_pattern(pattern: str | None) -> str:
    if not isinstance(pattern, str) or not pattern.strip():
        raise CodeSearchError("pattern cannot be empty")
    return pattern.strip().replace("\\", "/")


def _matches_glob(relative: str, name: str, pattern: str) -> bool:
    if fnmatch.fnmatch(relative, pattern) or fnmatch.fnmatch(name, pattern):
        return True
    return pattern.startswith("**/") and fnmatch.fnmatch(relative, pattern[3:])


def _display_path(workspace: Path, target: Path) -> str:
    try:
        return target.relative_to(workspace.resolve()).as_posix()
    except ValueError:
        return str(target)


def _rg_exclusion_arguments() -> list[str]:
    arguments: list[str] = []
    for directory in sorted(SEARCH_EXCLUDED_DIRECTORIES):
        arguments.extend(["--glob", f"!**/{directory}/**"])
    return arguments


def _start_rg(argv: list[str], cwd: Path) -> subprocess.Popen[str]:
    try:
        return subprocess.Popen(
            argv,
            cwd=cwd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            shell=False,
            creationflags=(subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0),
        )
    except OSError as exc:
        raise CodeSearchError(f"could not start ripgrep: {exc}") from exc


def _raise_if_cancelled(process: subprocess.Popen[str]) -> None:
    if not tool_cancellation_requested():
        return
    process.kill()
    raise CodeSearchError("task cancelled during code search")


def _read_process_error(process: subprocess.Popen[str]) -> str:
    if process.stderr is None:
        return ""
    return process.stderr.read().strip()[:2_000]


def _close_process(process: subprocess.Popen[str]) -> None:
    if process.poll() is None:
        process.kill()
    try:
        process.wait(timeout=2)
    except subprocess.TimeoutExpired:
        pass
    if process.stdout is not None:
        process.stdout.close()
    if process.stderr is not None:
        process.stderr.close()


def _is_reparse_point(status: os.stat_result) -> bool:
    attributes = getattr(status, "st_file_attributes", 0)
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    return bool(attributes & reparse_flag)
