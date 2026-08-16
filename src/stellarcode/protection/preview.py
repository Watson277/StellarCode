from __future__ import annotations

import difflib
import hashlib
import os
from pathlib import Path
from typing import Any

from stellarcode.protection.scope import snapshot_protection_reason


MAX_DIFF_CHARS = 24_000
_SENSITIVE_NAMES = {
    ".env",
    "credentials.json",
    "secrets.json",
    "id_rsa",
    "id_ed25519",
}
_SENSITIVE_SUFFIXES = {".key", ".pem", ".p12", ".pfx"}


def preview_write_file(root: Path, path: str, content: str) -> dict[str, Any]:
    target = _resolve_path(root, path)
    before = _read_existing(target)
    after = content.encode("utf-8")
    return _build_preview(root, target, before, after)


def preview_delete_file(root: Path, path: str) -> dict[str, Any]:
    lexical_target = _resolve_lexical_path(root, path)
    # Unlinking a symlink removes the link itself.  A path reached through a
    # symlinked parent, however, mutates the resolved external file and must be
    # displayed as outside the rollback boundary.
    target = lexical_target if lexical_target.is_symlink() else lexical_target.resolve()
    before = _read_existing(target)
    return _build_preview(root, target, before, None)


def _build_preview(
    root: Path,
    target: Path,
    before: bytes | None,
    after: bytes | None,
) -> dict[str, Any]:
    display_path = _display_path(root, target)
    if before is None and after is None:
        operation = "no_change"
    elif before is None:
        operation = "create"
    elif after is None:
        operation = "delete"
    elif before == after:
        operation = "no_change"
    else:
        operation = "modify"

    before_text, before_binary = _decode_text(before)
    after_text, after_binary = _decode_text(after)
    sensitive = _is_sensitive(target)
    protection_reason = snapshot_protection_reason(root, target)
    additions, deletions = _line_changes(before_text, after_text)
    binary = before_binary or after_binary
    if sensitive:
        diff = "[Diff hidden because this path may contain secrets.]"
        truncated = False
    elif binary:
        diff = "[Binary content changed; textual diff is unavailable.]"
        truncated = False
    elif operation == "no_change":
        diff = ""
        truncated = False
    else:
        from_name = "/dev/null" if before is None else f"a/{display_path}"
        to_name = "/dev/null" if after is None else f"b/{display_path}"
        lines = difflib.unified_diff(
            (before_text or "").splitlines(),
            (after_text or "").splitlines(),
            fromfile=from_name,
            tofile=to_name,
            lineterm="",
            n=3,
        )
        rendered = "\n".join(lines)
        diff, truncated = _truncate(rendered)

    return {
        "operation": operation,
        "path": display_path,
        "workspace_scoped": _is_within(root, target),
        "rollback_protected": protection_reason is None,
        "protection_reason": protection_reason,
        "sensitive": sensitive,
        "binary": binary,
        "before_sha256": _sha256(before),
        "after_sha256": _sha256(after),
        "additions": additions,
        "deletions": deletions,
        "diff": diff,
        "truncated": truncated,
    }


def _read_existing(path: Path) -> bytes | None:
    if not path.exists() and not path.is_symlink():
        return None
    if not path.is_file():
        raise ValueError(f"Path is not a file: {path}")
    return path.read_bytes()


def _decode_text(value: bytes | None) -> tuple[str | None, bool]:
    if value is None:
        return None, False
    if b"\x00" in value:
        return None, True
    try:
        return value.decode("utf-8"), False
    except UnicodeDecodeError:
        return None, True


def _line_changes(before: str | None, after: str | None) -> tuple[int, int]:
    if before is None and after is None:
        return 0, 0
    before_lines = (before or "").splitlines()
    after_lines = (after or "").splitlines()
    additions = 0
    deletions = 0
    matcher = difflib.SequenceMatcher(a=before_lines, b=after_lines, autojunk=False)
    for tag, left_start, left_end, right_start, right_end in matcher.get_opcodes():
        if tag in {"replace", "delete"}:
            deletions += left_end - left_start
        if tag in {"replace", "insert"}:
            additions += right_end - right_start
    return additions, deletions


def _resolve_path(root: Path, user_path: str) -> Path:
    raw = Path(user_path).expanduser()
    return raw.resolve() if raw.is_absolute() else (root / raw).resolve()


def _resolve_lexical_path(root: Path, user_path: str) -> Path:
    raw = Path(user_path).expanduser()
    candidate = raw if raw.is_absolute() else root / raw
    return Path(os.path.abspath(candidate))


def _display_path(root: Path, path: Path) -> str:
    try:
        return path.relative_to(root.resolve()).as_posix()
    except ValueError:
        return str(path)


def _is_within(root: Path, path: Path) -> bool:
    try:
        path.relative_to(root.resolve())
        return True
    except ValueError:
        return False


def _is_sensitive(path: Path) -> bool:
    name = path.name.lower()
    return (
        name in _SENSITIVE_NAMES
        or name.startswith(".env.")
        or path.suffix.lower() in _SENSITIVE_SUFFIXES
        or any(part.lower() in {".ssh", ".aws", ".azure"} for part in path.parts)
    )


def _sha256(value: bytes | None) -> str | None:
    return hashlib.sha256(value).hexdigest() if value is not None else None


def _truncate(value: str) -> tuple[str, bool]:
    if len(value) <= MAX_DIFF_CHARS:
        return value, False
    return value[:MAX_DIFF_CHARS] + "\n...[diff truncated]", True
