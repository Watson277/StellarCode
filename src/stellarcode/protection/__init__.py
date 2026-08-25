"""Workspace snapshots, isolated worktrees, previews, merges, and rollback."""

from stellarcode.protection.preview import preview_delete_file, preview_write_file
from stellarcode.protection.workspace import (
    WorkspaceProtectionError,
    WorkspaceProtectionService,
    WorkspaceRollbackConflict,
)

__all__ = [
    "WorkspaceProtectionError",
    "WorkspaceProtectionService",
    "WorkspaceRollbackConflict",
    "preview_delete_file",
    "preview_write_file",
]
