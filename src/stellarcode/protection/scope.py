from __future__ import annotations

from pathlib import Path


# These paths are intentionally outside task snapshots: they are either another
# Git database, StellarCode's own telemetry, dependency/build output that can be
# regenerated, or files which commonly contain secrets.  The Side-Git builder
# hashes source bytes directly with `git hash-object --no-filters`; these explicit
# exclusions therefore remain authoritative regardless of user .gitignore rules.
SNAPSHOT_EXCLUDED_DIRECTORIES = frozenset(
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
SNAPSHOT_SENSITIVE_NAMES = frozenset(
    {
        ".env",
        "credentials.json",
        "secrets.json",
        "id_rsa",
        "id_ed25519",
    }
)
SNAPSHOT_SENSITIVE_SUFFIXES = frozenset({".key", ".pem", ".p12", ".pfx"})


def snapshot_protection_reason(root: Path, path: Path) -> str | None:
    """Return a stable UI code when Side-Git intentionally excludes a path."""

    resolved_root = root.resolve()
    try:
        relative = path.relative_to(resolved_root)
    except ValueError:
        return "outside_workspace"
    return snapshot_relative_exclusion_reason(relative)


def snapshot_relative_exclusion_reason(relative: Path) -> str | None:
    """Return the exclusion code for a workspace-relative path."""

    lowered_parts = [part.lower() for part in relative.parts]
    if any(part in SNAPSHOT_EXCLUDED_DIRECTORIES for part in lowered_parts[:-1]) or (
        lowered_parts and lowered_parts[-1] in {".git", ".stellarcode"}
    ):
        return "generated_or_internal_path"
    name = relative.name.lower()
    if (
        name in SNAPSHOT_SENSITIVE_NAMES
        or name.startswith(".env.")
        or relative.suffix.lower() in SNAPSHOT_SENSITIVE_SUFFIXES
        or any(part in {".ssh", ".aws", ".azure"} for part in lowered_parts)
    ):
        return "sensitive_path"
    return None
