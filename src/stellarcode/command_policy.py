"""Conservative command classification used before terminal execution."""

from __future__ import annotations

import re


_SAFE_READ_ONLY_SEGMENTS = (
    re.compile(
        r"conda(?:\.exe)?\s+(?:--version|-v|env\s+list|info|list)(?:\s+--json)?",
        re.IGNORECASE,
    ),
    re.compile(
        r"conda(?:\.exe)?\s+run\s+(?:-n|--name)\s+[a-z0-9_.-]+\s+"
        r"python(?:\.exe)?\s+(?:--version|-v)",
        re.IGNORECASE,
    ),
    re.compile(
        r"conda(?:\.exe)?\s+run\s+(?:-n|--name)\s+[a-z0-9_.-]+\s+"
        r"(?:python(?:\.exe)?\s+-m\s+)?pip\s+"
        r"(?:--version|list|freeze|show(?:\s+[a-z0-9_.-]+)*)",
        re.IGNORECASE,
    ),
    re.compile(r"python(?:\.exe)?\s+(?:--version|-v)", re.IGNORECASE),
    re.compile(
        r"(?:python(?:\.exe)?\s+-m\s+)?pip\s+"
        r"(?:--version|list|freeze|show(?:\s+[a-z0-9_.-]+)*)",
        re.IGNORECASE,
    ),
    re.compile(
        r"(?:get-command|gcm)\s+[a-z0-9_.-]+"
        r"(?:\s+-erroraction\s+(?:silentlycontinue|continue|stop))?",
        re.IGNORECASE,
    ),
    re.compile(r"(?:get-location|pwd)", re.IGNORECASE),
    re.compile(r"(?:test-path|resolve-path)\s+.+", re.IGNORECASE),
    re.compile(r"(?:where|where\.exe)\s+[a-z0-9_.-]+", re.IGNORECASE),
    re.compile(r"nvidia-smi(?:\s+--[a-z0-9_.=-]+)*", re.IGNORECASE),
    re.compile(
        r"(?:select-object|select)\s+(?:-property\s+)?[a-z0-9_.*,-]+"
        r"(?:\s*,\s*[a-z0-9_.*-]+)*",
        re.IGNORECASE,
    ),
    re.compile(r"(?:format-list|fl)(?:\s+[a-z0-9_.*,-]+)?", re.IGNORECASE),
)


def is_safe_read_only_command(command: str | list[str]) -> bool:
    """Return true only for a small, whole-command inspection allowlist."""
    if isinstance(command, list):
        if not command or not all(isinstance(part, str) and part for part in command):
            return False
        command_text = " ".join(command)
    elif isinstance(command, str):
        command_text = command
    else:
        return False

    normalized = " ".join(command_text.strip().split())
    if not normalized:
        return False
    if any(token in normalized for token in ("&&", "||", ">", "<", "`", "$(", "&")):
        return False

    for statement in normalized.split(";"):
        statement = statement.strip()
        if not statement:
            return False
        segments = [segment.strip() for segment in statement.split("|")]
        if not all(_is_safe_segment(segment) for segment in segments):
            return False
    return True


def _is_safe_segment(segment: str) -> bool:
    return any(pattern.fullmatch(segment) for pattern in _SAFE_READ_ONLY_SEGMENTS)


def is_full_disk_recursive_scan(command: str | list[str]) -> bool:
    if isinstance(command, list):
        normalized = " ".join(command)
    else:
        normalized = command
    normalized = " ".join(normalized.strip().lower().split())

    if re.match(r"^find\s+(?:/|~|\$home)(?:\s|$)", normalized):
        return True

    windows_root = r"[a-z]:[\\/](?:[\"']?\s|[\"']?$)"
    if re.search(r"\b(?:get-childitem|gci)\b", normalized):
        return "-recurse" in normalized and bool(re.search(windows_root, normalized))
    if normalized.startswith("dir "):
        return "/s" in normalized and bool(re.search(windows_root, normalized))
    if normalized.startswith("where /r "):
        return bool(re.search(windows_root, normalized))
    return False


def restricted_scan_message() -> str:
    return (
        "[POLICY] Full-disk recursive scans are blocked in restricted mode. "
        "Narrow the target path or use list_dir, read_file, and search_code. "
        "Switch to /mode full-access only when an unrestricted scan is intentional."
    )
