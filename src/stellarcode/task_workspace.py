"""Context-local mapping from a canonical project to a task's temporary worktree.

``contextvars`` prevents concurrent tasks from accidentally resolving relative paths
into each other's worktrees while preserving the original project path for UI output.
"""

from __future__ import annotations

import contextvars
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


_TASK_WORKSPACE: contextvars.ContextVar[tuple[Path, Path] | None] = (
    contextvars.ContextVar("stellarcode_task_workspace", default=None)
)


@contextmanager
def task_workspace_scope(
    project_workspace: str | Path,
    task_workspace: str | Path | None,
) -> Iterator[None]:
    """Route project-relative file operations into one task's isolated worktree."""

    if task_workspace is None:
        yield
        return
    token = _TASK_WORKSPACE.set(
        (Path(project_workspace).resolve(), Path(task_workspace).resolve())
    )
    try:
        yield
    finally:
        _TASK_WORKSPACE.reset(token)


def effective_workspace(default_workspace: str | Path) -> Path:
    active = _TASK_WORKSPACE.get()
    if active is None:
        return Path(default_workspace).resolve()
    project_workspace, isolated_workspace = active
    default = Path(default_workspace).resolve()
    return isolated_workspace if default == project_workspace else default


def task_workspace_active(default_workspace: str | Path) -> bool:
    """Return whether project-relative operations are routed to a task worktree."""

    active = _TASK_WORKSPACE.get()
    if active is None:
        return False
    return Path(default_workspace).resolve() == active[0]


def resolve_task_path(default_workspace: str | Path, user_path: str) -> Path:
    """Resolve a path and remap absolute project paths into the active worktree."""

    project = Path(default_workspace).resolve()
    active = _TASK_WORKSPACE.get()
    raw = Path(user_path).expanduser()
    if raw.is_absolute():
        absolute = raw.resolve()
        if active is not None and project == active[0]:
            try:
                return (active[1] / absolute.relative_to(project)).resolve()
            except ValueError:
                pass
        return absolute
    root = active[1] if active is not None and project == active[0] else project
    return (root / raw).resolve()


def remap_task_path_lexically(default_workspace: str | Path, user_path: str) -> Path:
    """Remap without resolving the final component, preserving symlink deletion semantics."""

    project = Path(default_workspace).resolve()
    active = _TASK_WORKSPACE.get()
    raw = Path(user_path).expanduser()
    if raw.is_absolute():
        absolute = Path.absolute(raw)
        if active is not None and project == active[0]:
            try:
                return active[1] / absolute.relative_to(project)
            except ValueError:
                pass
        return absolute
    root = active[1] if active is not None and project == active[0] else project
    return root / raw


def remap_task_command(
    default_workspace: str | Path,
    command: str | list[str],
) -> str | list[str]:
    """Redirect explicit project-root strings in commands to the active worktree."""

    active = _TASK_WORKSPACE.get()
    project = Path(default_workspace).resolve()
    if active is None or project != active[0]:
        return command
    replacements = (
        (str(project), str(active[1])),
        (project.as_posix(), active[1].as_posix()),
    )

    def rewrite(value: str) -> str:
        updated = value
        for source, target in replacements:
            updated = updated.replace(source, target)
        return updated

    return rewrite(command) if isinstance(command, str) else [rewrite(value) for value in command]
