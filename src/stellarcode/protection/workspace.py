from __future__ import annotations

import hashlib
import json
import os
import re
import signal
import shutil
import stat
import subprocess
import tempfile
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from stellarcode.protection.scope import snapshot_relative_exclusion_reason


MAX_DIFF_REQUEST_CHARS = 200_000
MAX_TASK_RECORDS = 100


class WorkspaceProtectionError(RuntimeError):
    """Raised when a workspace snapshot or restore cannot be completed safely."""


class WorkspaceRollbackConflict(WorkspaceProtectionError):
    def __init__(self, paths: list[str], message: str | None = None) -> None:
        self.paths = paths
        preview = ", ".join(paths[:5])
        suffix = "" if len(paths) <= 5 else f" and {len(paths) - 5} more"
        super().__init__(
            message
            or (
                "Rollback stopped because task-modified files changed afterwards: "
                f"{preview}{suffix}. Review or save those edits before retrying."
            )
        )


class _ProjectStorageLock:
    """Cross-process ownership lock for one project's Side-Git state.

    The snapshot index is deliberately shared by every conversation in a
    project, but it must never be driven by two Runtime processes at once.  An
    OS-backed advisory lock is released automatically when a process crashes;
    the small file remains only as diagnosable ownership metadata.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # One create-or-open operation avoids two starters racing through a
        # FileNotFoundError/w+b sequence and truncating each other's lock file.
        self._stream = path.open("a+b")
        self._locked = False
        try:
            self._stream.seek(0, os.SEEK_END)
            if self._stream.tell() == 0:
                self._stream.write(b"\0")
                self._stream.flush()
            self._stream.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(self._stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(self._stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            self._locked = True
            metadata = f"pid={os.getpid()} acquired_at={_timestamp()}\n".encode("ascii")
            self._stream.seek(0)
            self._stream.truncate()
            self._stream.write(metadata)
            self._stream.flush()
            os.fsync(self._stream.fileno())
        except (OSError, BlockingIOError) as exc:
            self._stream.close()
            raise WorkspaceProtectionError(
                "Workspace modification protection is already owned by another "
                "StellarCode Runtime for this project. Close the other client and retry."
            ) from exc

    def close(self) -> None:
        if not self._locked:
            return
        try:
            self._stream.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(self._stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(self._stream.fileno(), fcntl.LOCK_UN)
        finally:
            self._locked = False
            self._stream.close()


class WorkspaceProtectionService:
    """Task-scoped Side-Git snapshots that never touch the user's Git index or refs."""

    def __init__(self, workspace: str | Path, storage_dir: str | Path) -> None:
        self.workspace = Path(workspace).resolve()
        self.storage_dir = Path(storage_dir).resolve()
        self.repository_dir = self.storage_dir / "objects.git"
        self.index_file = self.storage_dir / "snapshot.index"
        self.records_dir = self.storage_dir / "tasks"
        self.git = shutil.which("git")
        self._lock = threading.RLock()
        self._storage_lock: _ProjectStorageLock | None = None
        self._unavailable_reason = ""
        self.storage_dir.mkdir(parents=True, exist_ok=True)
        self.records_dir.mkdir(parents=True, exist_ok=True)
        try:
            self._storage_lock = _ProjectStorageLock(
                self.storage_dir / "workspace-protection.lock"
            )
        except WorkspaceProtectionError as exc:
            self._unavailable_reason = str(exc)
            return
        if self.git is None:
            self._unavailable_reason = "Git executable was not found."
            self.close()
            return
        try:
            self._initialize_repository()
            self._reconcile_incomplete_rollbacks()
            self._prune_records()
        except WorkspaceProtectionError as exc:
            self._unavailable_reason = str(exc)
            self.close()

    @property
    def available(self) -> bool:
        return (
            self.git is not None
            and self._storage_lock is not None
            and not self._unavailable_reason
        )

    def close(self) -> None:
        """Release the project-wide Side-Git ownership lock."""

        with self._lock:
            storage_lock = self._storage_lock
            self._storage_lock = None
            if storage_lock is not None:
                storage_lock.close()

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:
            # Destructors run during interpreter teardown, when imported modules
            # and file handles may already be partially finalized.
            pass

    def begin_task(self, task_id: str, session_id: str) -> dict[str, Any]:
        with self._lock:
            existing = self._load_record(task_id)
            if existing is not None:
                if str(existing.get("session_id") or "") != session_id:
                    raise WorkspaceProtectionError("Task snapshot belongs to another session.")
                return self._public_record(existing)
            if not self.available:
                return self._unprotected(task_id, session_id)

            token = _task_token(task_id)
            before = self._capture_commit(
                f"StellarCode task baseline {task_id}",
                f"refs/stellarcode/tasks/{token}/before",
            )
            overlapping = self._active_records(exclude_task_id=task_id)
            overlapping_task_ids = sorted(
                str(item.get("task_id") or "")
                for item in overlapping
                if item.get("task_id")
            )
            record: dict[str, Any] = {
                "schema_version": 1,
                "snapshot_id": f"snapshot-{uuid.uuid4().hex}",
                "task_id": task_id,
                "session_id": session_id,
                "workspace": str(self.workspace),
                "backend": "side-git",
                "protected": True,
                "status": "active",
                "before_revision": before,
                "after_revision": None,
                "changed_files": [],
                "additions": 0,
                "deletions": 0,
                "has_changes": False,
                "rollback_available": False,
                "concurrent_task_ids": overlapping_task_ids,
                "change_attribution": (
                    "shared_workspace_overlap" if overlapping_task_ids else "task"
                ),
                "rollback_block_reason": (
                    "Rollback is unavailable because another conversation task overlapped "
                    "this task in the same workspace."
                    if overlapping_task_ids
                    else ""
                ),
                "rolled_back": False,
                "created_at": _timestamp(),
                "updated_at": _timestamp(),
            }
            self._write_record(record)
            for active in overlapping:
                peers = {
                    str(value)
                    for value in active.get("concurrent_task_ids") or []
                    if value
                }
                peers.add(task_id)
                active.update(
                    concurrent_task_ids=sorted(peers),
                    change_attribution="shared_workspace_overlap",
                    rollback_available=False,
                    rollback_block_reason=(
                        "Rollback is unavailable because another conversation task "
                        "overlapped this task in the same workspace."
                    ),
                    updated_at=_timestamp(),
                )
                self._write_record(active)
            return self._public_record(record)

    def finalize_task(self, task_id: str, outcome: str) -> dict[str, Any]:
        with self._lock:
            self._require_available()
            record = self._load_record(task_id)
            if record is None:
                return self._unprotected(task_id, "", "Task baseline snapshot is unavailable.")
            if not record.get("protected"):
                return self._public_record(record)
            if record.get("status") != "active" and record.get("after_revision"):
                return self._public_record(record)

            token = _task_token(task_id)
            after_ref = f"refs/stellarcode/tasks/{token}/after"
            # update-ref is durable before the task record is replaced.  If the
            # process stops in that narrow window, reuse the already captured
            # POST_TASK tree instead of taking a new snapshot that could absorb
            # edits made after the crash.
            after = self._existing_snapshot_ref(after_ref) or self._capture_commit(
                f"StellarCode task result {task_id}",
                after_ref,
            )
            before = str(record["before_revision"])
            details = self._diff_details(before, after)
            record.update(
                status=outcome,
                outcome=outcome,
                after_revision=after,
                changed_files=details["changed_files"],
                additions=details["additions"],
                deletions=details["deletions"],
                has_changes=bool(details["changed_files"]),
                rollback_available=bool(details["changed_files"])
                and not bool(record.get("concurrent_task_ids")),
                completed_at=_timestamp(),
                updated_at=_timestamp(),
            )
            self._write_record(record)
            self._prune_records()
            return self._public_record(record)

    def rollback_task(
        self,
        task_id: str,
        session_id: str,
        snapshot_id: str | None = None,
    ) -> dict[str, Any]:
        with self._lock:
            self._require_available()
            record = self._load_record(task_id)
            if record is None:
                raise WorkspaceProtectionError("Task change snapshot was not found.")
            if str(record.get("session_id") or "") != session_id:
                raise WorkspaceProtectionError("Task change snapshot belongs to another session.")
            if snapshot_id and str(record.get("snapshot_id") or "") != snapshot_id:
                raise WorkspaceProtectionError("Task change snapshot is stale.")
            if not record.get("protected"):
                raise WorkspaceProtectionError(
                    str(record.get("error") or "This task was not protected by a Git snapshot.")
                )
            if record.get("status") == "active":
                raise WorkspaceProtectionError("Stop the active task before rolling it back.")
            if record.get("rolled_back"):
                raise WorkspaceProtectionError("This task has already been rolled back.")
            if record.get("concurrent_task_ids"):
                raise WorkspaceProtectionError(
                    str(
                        record.get("rollback_block_reason")
                        or "Rollback is unavailable for overlapping workspace tasks."
                    )
                )
            if record.get("rollback_state") == "recovery_failed":
                raise WorkspaceProtectionError(
                    str(
                        record.get("rollback_error")
                        or "An interrupted rollback could not be recovered automatically."
                    )
                )
            changed_paths = [str(item["path"]) for item in record.get("changed_files") or []]
            if not changed_paths:
                raise WorkspaceProtectionError("This task did not change protected workspace files.")

            token = _task_token(task_id)
            current = self._capture_commit(
                f"StellarCode pre-rollback safety {task_id}",
                f"refs/stellarcode/tasks/{token}/rollback-safety",
            )
            after = str(record.get("after_revision") or "")
            conflicts = self._path_intersection(
                changed_paths,
                self._changed_paths(after, current),
            )
            if conflicts:
                raise WorkspaceRollbackConflict(conflicts)
            ads_conflicts = self._paths_with_alternate_streams(changed_paths)
            if ads_conflicts:
                raise WorkspaceRollbackConflict(
                    ads_conflicts,
                    "Rollback stopped because affected files contain Windows alternate "
                    "data streams that are outside the Side-Git snapshot: "
                    f"{', '.join(ads_conflicts[:5])}. Remove or save those streams before retrying.",
                )
            self._assert_restore_will_not_delete_unprotected(
                str(record["before_revision"]),
                current,
                changed_paths,
            )

            record.update(
                rollback_state="in_progress",
                rollback_safety_revision=current,
                rollback_error="",
                rollback_recovery_event_pending=False,
                updated_at=_timestamp(),
            )
            self._write_record(record)
            mutation_started = False
            try:
                # Close the ordinary editor race window immediately before the first
                # workspace mutation. A safety commit also provides the expected
                # byte/type state for this second compare-and-swap check.
                latest = self._capture_commit(
                    f"StellarCode rollback compare-and-swap {task_id}",
                    f"refs/stellarcode/tasks/{token}/rollback-cas",
                )
                late_conflicts = self._path_intersection(
                    changed_paths,
                    self._changed_paths(current, latest),
                )
                late_ads_conflicts = self._paths_with_alternate_streams(changed_paths)
                if late_conflicts or late_ads_conflicts:
                    raise WorkspaceRollbackConflict(
                        sorted(set(late_conflicts + late_ads_conflicts))
                    )
                self._assert_restore_will_not_delete_unprotected(
                    str(record["before_revision"]),
                    latest,
                    changed_paths,
                )
                mutation_started = True
                self._restore_paths(
                    str(record["before_revision"]),
                    changed_paths,
                    expected_revision=latest,
                )
                restored = self._capture_commit(
                    f"StellarCode rollback result {task_id}",
                    f"refs/stellarcode/tasks/{token}/rollback-result",
                )
            except Exception as exc:
                if not mutation_started:
                    # A late compare-and-swap/ADS/preflight conflict occurred
                    # before StellarCode touched the workspace.  Never run the
                    # safety compensation here: doing so would overwrite the very
                    # user edit that the second check just detected.
                    record.update(
                        rollback_state="failed",
                        rollback_error=f"{type(exc).__name__}: {exc}",
                        rollback_available=True,
                        rollback_recovery_event_pending=False,
                        updated_at=_timestamp(),
                    )
                    self._write_record(record)
                    if isinstance(exc, WorkspaceProtectionError):
                        raise
                    raise WorkspaceProtectionError(f"Rollback failed: {exc}") from exc
                compensation_error: Exception | None = None
                emergency_revision = ""
                try:
                    # A restore may have partially succeeded before a later write
                    # or the result snapshot failed.  Re-read the live workspace
                    # before compensating.  Only a path-by-path mixture of the
                    # PRE_TASK and pre-rollback safety states is safe to restore;
                    # any third state may be a concurrent editor change and must
                    # be preserved for manual recovery.
                    emergency_revision = self._capture_commit(
                        f"StellarCode failed rollback emergency {task_id}",
                        f"refs/stellarcode/tasks/{token}/rollback-emergency",
                    )
                    if not self._is_safe_partial_rollback_state(
                        emergency_revision,
                        current,
                        str(record["before_revision"]),
                        changed_paths,
                    ):
                        raise WorkspaceProtectionError(
                            "The workspace changed while rollback was being finalized; "
                            "the current files were preserved in an emergency snapshot."
                        )
                    emergency_ads = self._paths_with_alternate_streams(changed_paths)
                    if emergency_ads:
                        raise WorkspaceProtectionError(
                            "Windows alternate data streams appeared during rollback; "
                            "the current files were preserved for manual recovery."
                        )
                    self._assert_restore_will_not_delete_unprotected(
                        current,
                        emergency_revision,
                        changed_paths,
                    )
                    self._restore_paths(
                        current,
                        changed_paths,
                        expected_revision=emergency_revision,
                    )
                except Exception as compensation_exc:
                    compensation_error = compensation_exc
                if compensation_error is None:
                    record.update(
                        rollback_state="failed",
                        rollback_error=f"{type(exc).__name__}: {exc}",
                        rollback_available=True,
                        rollback_recovery_event_pending=True,
                        updated_at=_timestamp(),
                    )
                else:
                    record.update(
                        rollback_state="recovery_failed",
                        rollback_error=(
                            f"Rollback failed: {type(exc).__name__}: {exc}; restoring the "
                            "pre-rollback safety snapshot also failed: "
                            f"{type(compensation_error).__name__}: {compensation_error}"
                        ),
                        rollback_available=False,
                        rollback_recovery_event_pending=True,
                        **(
                            {"rollback_emergency_revision": emergency_revision}
                            if emergency_revision
                            else {}
                        ),
                        updated_at=_timestamp(),
                    )
                self._write_record(record)
                if compensation_error is not None:
                    raise WorkspaceProtectionError(str(record["rollback_error"])) from exc
                if isinstance(exc, WorkspaceProtectionError):
                    raise
                raise WorkspaceProtectionError(f"Rollback failed: {exc}") from exc

            record.update(
                rollback_state="completed",
                rollback_result_revision=restored,
                rollback_available=False,
                rolled_back=True,
                rolled_back_at=_timestamp(),
                restored_files=changed_paths,
                updated_at=_timestamp(),
            )
            self._write_record(record)
            result = self._public_record(record)
            result["restored_files"] = changed_paths
            return result

    def pending_rollback_recoveries(self) -> list[dict[str, Any]]:
        """Return durable recovery notices that have not reached the event journal yet."""

        with self._lock:
            pending: list[dict[str, Any]] = []
            for path in self.records_dir.glob("*.json"):
                try:
                    payload = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError, TypeError, ValueError):
                    continue
                if isinstance(payload, dict) and payload.get(
                    "rollback_recovery_event_pending"
                ):
                    pending.append(self._public_record(payload))
            return pending

    def acknowledge_rollback_recovery(self, task_id: str) -> None:
        """Clear a recovery notice only after its failure event was journaled."""

        with self._lock:
            record = self._load_record(task_id)
            if record is None or not record.get("rollback_recovery_event_pending"):
                return
            record["rollback_recovery_event_pending"] = False
            record["updated_at"] = _timestamp()
            self._write_record(record)

    def task_status(self, task_id: str) -> dict[str, Any]:
        with self._lock:
            record = self._load_record(task_id)
            if record is None:
                return self._unprotected(task_id, "", "Task snapshot is unavailable.")
            return self._public_record(record)

    def validate_recovery_baseline(
        self,
        task_id: str,
        session_id: str,
        snapshot_id: str,
    ) -> dict[str, Any]:
        """Fail closed unless an unfinished task still owns a usable PRE snapshot."""

        with self._lock:
            self._require_available()
            record = self._load_record(task_id)
            if record is None:
                raise WorkspaceProtectionError(
                    "The unfinished task cannot be recovered because its protection record "
                    "is missing. Discard the interrupted task instead of resuming it unprotected."
                )
            if not record.get("protected"):
                raise WorkspaceProtectionError(
                    str(record.get("error") or "The unfinished task has no protected baseline.")
                )
            if str(record.get("status") or "") != "active" or record.get("after_revision"):
                raise WorkspaceProtectionError(
                    "The task already has a final workspace snapshot and must not resume "
                    "Agent execution. Only its pending terminal event may be finalized."
                )
            if str(record.get("session_id") or "") != session_id:
                raise WorkspaceProtectionError(
                    "The unfinished task snapshot belongs to another conversation."
                )
            if not snapshot_id or str(record.get("snapshot_id") or "") != snapshot_id:
                raise WorkspaceProtectionError(
                    "The unfinished task checkpoint does not match its protection snapshot."
                )
            before_revision = str(record.get("before_revision") or "")
            if not before_revision:
                raise WorkspaceProtectionError(
                    "The unfinished task protection record has no baseline revision."
                )
            # A JSON record alone is insufficient: retention, manual cleanup, or disk
            # corruption may have removed the underlying Git object.  Verify it before
            # any recovered agent/tool code is allowed to run.
            self._run_git("cat-file", "-e", f"{before_revision}^{{tree}}")
            return self._public_record(record)

    def task_diff(
        self,
        task_id: str,
        session_id: str,
        *,
        max_chars: int = 80_000,
    ) -> dict[str, Any]:
        with self._lock:
            self._require_available()
            record = self._load_record(task_id)
            if record is None:
                raise WorkspaceProtectionError("Task change snapshot was not found.")
            if str(record.get("session_id") or "") != session_id:
                raise WorkspaceProtectionError("Task change snapshot belongs to another session.")
            if record.get("concurrent_task_ids"):
                raise WorkspaceProtectionError(
                    str(
                        record.get("rollback_block_reason")
                        or "Task diff attribution is unavailable for overlapping workspace tasks."
                    )
                )
            before = str(record.get("before_revision") or "")
            after = str(record.get("after_revision") or "")
            if not before or not after:
                raise WorkspaceProtectionError("Task diff is not available until the task finishes.")
            limit = min(MAX_DIFF_REQUEST_CHARS, max(1_000, int(max_chars)))
            patch = self._run_git(
                "diff",
                "--no-ext-diff",
                "--no-color",
                "--no-renames",
                "--unified=3",
                before,
                after,
                "--",
            )
            truncated = len(patch) > limit
            if truncated:
                patch = patch[:limit] + "\n...[task diff truncated]"
            return {
                **self._public_record(record),
                "diff": patch,
                "diff_truncated": truncated,
            }

    def _initialize_repository(self) -> None:
        if self.git is None:
            raise WorkspaceProtectionError("Git executable was not found.")
        if not (self.repository_dir / "HEAD").exists():
            self.repository_dir.parent.mkdir(parents=True, exist_ok=True)
            self._run_plain_git("init", "--bare", str(self.repository_dir))
        self._run_git("config", "core.autocrlf", "false")
        self._run_git("config", "core.safecrlf", "false")
        self._run_git("config", "core.filemode", "true")

    def _require_available(self) -> None:
        if not self.available:
            raise WorkspaceProtectionError(
                self._unavailable_reason or "Workspace modification protection is unavailable."
            )

    def _reconcile_incomplete_rollbacks(self) -> None:
        """Undo a partially applied rollback after a hard Runtime crash.

        `rollback_safety_revision` is captured before the first workspace write.
        Restoring just the task paths is idempotent, so another crash during this
        reconciliation can safely retry it on the next launch.
        """

        for path in self.records_dir.glob("*.json"):
            try:
                record = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError, TypeError, ValueError):
                continue
            if not isinstance(record, dict) or record.get("rollback_state") != "in_progress":
                continue
            task_id = str(record.get("task_id") or "")
            changed_paths = [
                str(item.get("path") or "")
                for item in record.get("changed_files") or []
                if isinstance(item, dict) and item.get("path")
            ]
            safety_revision = str(record.get("rollback_safety_revision") or "")
            try:
                if not task_id or not safety_revision or not changed_paths:
                    raise WorkspaceProtectionError(
                        "Interrupted rollback safety metadata is incomplete."
                    )
                token = _task_token(task_id)
                emergency_revision = self._capture_commit(
                    f"StellarCode interrupted rollback emergency {task_id}",
                    f"refs/stellarcode/tasks/{token}/rollback-emergency",
                )
                before_revision = str(record.get("before_revision") or "")
                if not before_revision:
                    raise WorkspaceProtectionError(
                        "Interrupted rollback baseline metadata is incomplete."
                    )
                if not self._is_safe_partial_rollback_state(
                    emergency_revision,
                    safety_revision,
                    before_revision,
                    changed_paths,
                ):
                    raise WorkspaceProtectionError(
                        "Affected files were edited after the Runtime stopped; the current "
                        "workspace was preserved in an emergency snapshot."
                    )
                self._assert_restore_will_not_delete_unprotected(
                    safety_revision,
                    emergency_revision,
                    changed_paths,
                )
                self._restore_paths(
                    safety_revision,
                    changed_paths,
                    expected_revision=emergency_revision,
                )
                recovered_revision = self._capture_commit(
                    f"StellarCode interrupted rollback recovery {task_id}",
                    f"refs/stellarcode/tasks/{token}/rollback-recovered",
                )
                record.update(
                    rollback_state="failed",
                    rollback_recovered_revision=recovered_revision,
                    rollback_emergency_revision=emergency_revision,
                    rollback_error=(
                        "The Runtime stopped during rollback. StellarCode restored the "
                        "pre-rollback safety snapshot; the rollback can be retried."
                    ),
                    rollback_available=True,
                    rollback_recovery_event_pending=True,
                    updated_at=_timestamp(),
                )
            except Exception as exc:
                record.update(
                    rollback_state="recovery_failed",
                    rollback_error=(
                        "Interrupted rollback recovery failed; inspect the affected files "
                        f"before continuing: {type(exc).__name__}: {exc}"
                    ),
                    rollback_available=False,
                    rollback_recovery_event_pending=True,
                    updated_at=_timestamp(),
                )
            self._write_record(record)

    def _is_safe_partial_rollback_state(
        self,
        current_revision: str,
        safety_revision: str,
        before_revision: str,
        changed_paths: list[str],
    ) -> bool:
        current_tree = self._tree_entries(current_revision)
        safety_tree = self._tree_entries(safety_revision)
        before_tree = self._tree_entries(before_revision)
        relevant = {
            path
            for path in current_tree.keys() | safety_tree.keys() | before_tree.keys()
            if any(self._paths_overlap(path, expected) for expected in changed_paths)
        }
        return all(
            current_tree.get(path) in {safety_tree.get(path), before_tree.get(path)}
            for path in relevant
        )

    def _capture_commit(self, message: str, ref: str) -> str:
        self._run_git("read-tree", "--empty")
        entries = self._snapshot_entries()
        regular_entries = [entry for entry in entries if entry[1] != "120000"]
        object_ids = self._hash_regular_files([entry[0] for entry in regular_entries])
        index_rows: list[bytes] = []
        regular_hashes = iter(object_ids)
        for relative, mode, source in entries:
            object_id = (
                self._hash_blob(os.readlink(source).encode("utf-8", errors="surrogateescape"))
                if mode == "120000"
                else next(regular_hashes)
            )
            path_bytes = relative.encode("utf-8", errors="surrogateescape")
            index_rows.append(
                f"{mode} blob {object_id}\t".encode("ascii") + path_bytes + b"\0"
            )
        if index_rows:
            self._run_process(
                ("update-index", "-z", "--index-info"),
                environment=self._git_environment(),
                input_bytes=b"".join(index_rows),
            )
        tree = self._run_git("write-tree").strip()
        commit = self._run_git("commit-tree", tree, input_text=message + "\n").strip()
        self._run_git("update-ref", ref, commit)
        return commit

    def _existing_snapshot_ref(self, ref: str) -> str | None:
        """Resolve one exact Side-Git ref without treating absence as an error."""

        # These refs are created exclusively by StellarCode through update-ref.
        # Read the exact loose/packed ref directly instead of starting
        # `git for-each-ref`: a transient Git-for-Windows process stall here once
        # kept an already-cancelled task in finalization for the generic 90 s
        # snapshot timeout.
        parts = ref.split("/")
        if (
            not ref.startswith("refs/stellarcode/tasks/")
            or any(not part or part in {".", ".."} for part in parts)
        ):
            raise WorkspaceProtectionError(f"Snapshot ref is invalid: {ref}")
        loose_ref = self.repository_dir.joinpath(*parts)
        commit: str | None = None
        try:
            if loose_ref.is_file():
                commit = loose_ref.read_text(encoding="ascii").strip()
        except OSError as exc:
            raise WorkspaceProtectionError(f"Snapshot ref could not be read: {ref}") from exc
        if commit is None:
            packed_refs = self.repository_dir / "packed-refs"
            try:
                lines = (
                    packed_refs.read_text(encoding="ascii", errors="strict").splitlines()
                    if packed_refs.is_file()
                    else []
                )
            except (OSError, UnicodeError) as exc:
                raise WorkspaceProtectionError("Packed snapshot refs could not be read.") from exc
            matches = []
            for line in lines:
                if not line or line.startswith(("#", "^")):
                    continue
                object_id, separator, ref_name = line.partition(" ")
                if separator and ref_name == ref:
                    matches.append(object_id.strip())
            if len(matches) > 1:
                raise WorkspaceProtectionError(f"Snapshot ref is ambiguous: {ref}")
            commit = matches[0] if matches else None
        if commit is None:
            return None
        if re.fullmatch(r"[0-9a-fA-F]{40,64}", commit) is None:
            raise WorkspaceProtectionError(f"Snapshot ref is corrupt: {ref}")
        # A corrupt/non-commit ref must fail closed rather than silently causing
        # a fresh snapshot with different workspace contents.
        self._run_git("cat-file", "-e", f"{commit}^{{tree}}", timeout_seconds=5)
        return commit

    def _snapshot_entries(self) -> list[tuple[str, str, Path]]:
        entries: list[tuple[str, str, Path]] = []

        def visit(directory: Path, relative_directory: Path) -> None:
            try:
                children = sorted(os.scandir(directory), key=lambda item: item.name)
            except OSError as exc:
                raise WorkspaceProtectionError(
                    f"Could not scan workspace for a task snapshot: {directory}: {exc}"
                ) from exc
            for child in children:
                source = Path(child.path)
                relative = relative_directory / child.name
                if self._is_protection_storage_path(source):
                    continue
                if snapshot_relative_exclusion_reason(relative) is not None:
                    continue
                try:
                    if child.is_symlink():
                        entries.append((relative.as_posix(), "120000", source))
                        continue
                    file_stat = child.stat(follow_symlinks=False)
                    # Windows directory junctions and other non-symlink reparse
                    # points report is_symlink=False.  Never traverse or hash them:
                    # their target may be outside the selected workspace.
                    if _is_reparse_point(file_stat):
                        continue
                    try:
                        source.resolve().relative_to(self.workspace)
                    except ValueError:
                        # Defense in depth for platform-specific directory aliases
                        # that are not exposed as ordinary symbolic links.
                        continue
                    if child.is_dir(follow_symlinks=False):
                        visit(source, relative)
                    elif child.is_file(follow_symlinks=False):
                        mode = "100755" if file_stat.st_mode & stat.S_IXUSR else "100644"
                        entries.append((relative.as_posix(), mode, source))
                except OSError as exc:
                    raise WorkspaceProtectionError(
                        f"Could not inspect workspace path for a task snapshot: {source}: {exc}"
                    ) from exc

        visit(self.workspace, Path())
        entries.sort(key=lambda item: item[0])
        return entries

    def _is_protection_storage_path(self, path: Path) -> bool:
        try:
            path.resolve().relative_to(self.storage_dir)
            return True
        except ValueError:
            return False

    def _hash_regular_files(self, paths: list[str]) -> list[str]:
        if not paths:
            return []
        if any("\n" in path or "\r" in path for path in paths):
            raise WorkspaceProtectionError(
                "Workspace paths containing line breaks are not supported by task snapshots."
            )
        encoded_paths = b"".join(
            path.encode("utf-8", errors="surrogateescape") + b"\n" for path in paths
        )
        output = self._run_process(
            ("hash-object", "-w", "--stdin-paths", "--no-filters"),
            environment=self._git_environment(),
            input_bytes=encoded_paths,
        )
        object_ids = output.decode("ascii").splitlines()
        if len(object_ids) != len(paths):
            raise WorkspaceProtectionError(
                "Git did not hash every workspace file for the task snapshot."
            )
        return object_ids

    def _hash_blob(self, content: bytes) -> str:
        return self._run_process(
            ("hash-object", "-w", "--stdin", "--no-filters"),
            environment=self._git_environment(),
            input_bytes=content,
        ).decode("ascii").strip()

    def _diff_details(self, before: str, after: str) -> dict[str, Any]:
        paths = self._changed_paths(before, after)
        stats = self._numstat(before, after)
        statuses = self._name_status(before, after)
        additions = sum(item[1] for item in stats.values())
        deletions = sum(item[2] for item in stats.values())
        changed_files = [
            {
                "path": path,
                "status": statuses.get(path, stats.get(path, ("modified", 0, 0))[0]),
                "additions": stats.get(path, ("modified", 0, 0))[1],
                "deletions": stats.get(path, ("modified", 0, 0))[2],
            }
            for path in paths
        ]
        return {
            "changed_files": changed_files,
            "additions": additions,
            "deletions": deletions,
        }

    def _name_status(self, before: str, after: str) -> dict[str, str]:
        output = self._run_git_bytes(
            "diff",
            "--name-status",
            "-z",
            "--no-renames",
            before,
            after,
            "--",
        )
        fields = [item for item in output.split(b"\0") if item]
        result: dict[str, str] = {}
        labels = {
            "A": "created",
            "D": "deleted",
            "M": "modified",
            "T": "type_changed",
        }
        for index in range(0, len(fields) - 1, 2):
            code = fields[index].decode("ascii", errors="replace")[:1]
            path = fields[index + 1].decode(
                "utf-8", errors="surrogateescape"
            ).replace("\\", "/")
            result[path] = labels.get(code, "modified")
        return result

    def _changed_paths(self, before: str, after: str) -> list[str]:
        if not before or not after:
            return []
        output = self._run_git_bytes(
            "diff",
            "--name-only",
            "-z",
            "--no-renames",
            before,
            after,
            "--",
        )
        return sorted(
            {
                item.decode("utf-8", errors="surrogateescape").replace("\\", "/")
                for item in output.split(b"\0")
                if item
            }
        )

    def _numstat(self, before: str, after: str) -> dict[str, tuple[str, int, int]]:
        output = self._run_git_bytes(
            "diff",
            "--numstat",
            "-z",
            "--no-renames",
            before,
            after,
            "--",
        )
        result: dict[str, tuple[str, int, int]] = {}
        for record in output.split(b"\0"):
            if not record:
                continue
            parts = record.split(b"\t", 2)
            if len(parts) != 3:
                continue
            added_raw, deleted_raw, path_raw = parts
            path = path_raw.decode("utf-8", errors="surrogateescape").replace("\\", "/")
            added = int(added_raw) if added_raw.isdigit() else 0
            deleted = int(deleted_raw) if deleted_raw.isdigit() else 0
            status = "binary" if added_raw == b"-" or deleted_raw == b"-" else "modified"
            result[path] = (status, added, deleted)
        return result

    def _restore_paths(
        self,
        revision: str,
        paths: list[str],
        *,
        expected_revision: str | None = None,
    ) -> None:
        tree = self._tree_entries(revision)
        expected_tree = self._tree_entries(expected_revision) if expected_revision else None
        mutated_roots: set[str] = set()
        desired = {
            relative: tree.get(relative)
            for relative in paths
            if tree.get(relative) is not None
        }
        # First remove files/directories that obstruct the desired target types.
        # This is required for both file -> directory and directory -> file task
        # changes. Conflict checks happen before this method, and any ordinary
        # failure is compensated from the rollback safety revision.
        for relative in sorted(desired, key=lambda value: value.count("/")):
            if any(
                self._paths_overlap(relative, root) for root in mutated_roots
            ):
                continue
            self._assert_path_matches_expected(relative, expected_tree, mutated_roots)
            target = self._safe_workspace_path(relative)
            if target.is_file() and any(
                path.startswith(relative.rstrip("/") + "/") for path in desired
            ):
                # The task state is a file while PRE_TASK needs descendants.
                # Keep the file intact for the per-path CAS; the first child write
                # replaces it through _remove_parent_blockers.
                continue
            self._remove_parent_blockers(target)
            if target.is_symlink():
                target.unlink()
                mutated_roots.add(relative)
            elif target.exists() and target.is_dir():
                if _path_is_reparse_point(target):
                    raise WorkspaceProtectionError(
                        f"Refusing to recursively replace a reparse point: {relative}"
                    )
                self._assert_live_directory_matches_expected(
                    relative,
                    target,
                    expected_tree,
                )
                shutil.rmtree(target)
                mutated_roots.add(relative)

        for relative in sorted(
            (path for path in paths if path not in desired),
            key=lambda value: value.count("/"),
            reverse=True,
        ):
            if any(
                path.startswith(relative.rstrip("/") + "/") for path in desired
            ):
                # This exact path is absent in the PRE_TASK flat tree only because
                # PRE_TASK contains descendants below it. The descendant restore
                # will replace a task-state file parent safely.
                continue
            self._assert_path_matches_expected(relative, expected_tree, mutated_roots)
            target = self._safe_workspace_path(relative)
            self._delete_restored_path(target)
            mutated_roots.add(relative)

        for relative in sorted(desired, key=lambda value: value.count("/")):
            self._assert_path_matches_expected(relative, expected_tree, mutated_roots)
            target = self._safe_workspace_path(relative)
            entry = desired[relative]
            assert entry is not None
            mode, object_type, object_id = entry
            if object_type != "blob":
                raise WorkspaceProtectionError(
                    f"Cannot restore unsupported Git object {object_type}: {relative}"
                )
            content = self._run_git_bytes("cat-file", "blob", object_id)
            if mode == "120000":
                self._replace_symlink(target, content)
            else:
                self._atomic_write(target, content, executable=mode == "100755")

    def _assert_path_matches_expected(
        self,
        relative: str,
        expected_tree: dict[str, tuple[str, str, str]] | None,
        mutated_roots: set[str],
    ) -> None:
        if expected_tree is None or any(
            self._paths_overlap(relative, root) for root in mutated_roots
        ):
            return
        target = self._safe_workspace_path(relative)
        expected = self._expected_live_entry(expected_tree, relative)
        actual = self._live_entry(target)
        if actual != expected:
            raise WorkspaceRollbackConflict(
                [relative],
                "Rollback stopped because a task path changed while files were being "
                f"restored: {relative}. The late edit was preserved.",
            )

    @staticmethod
    def _expected_live_entry(
        tree: dict[str, tuple[str, str, str]],
        relative: str,
    ) -> tuple[str, str, str] | None:
        entry = tree.get(relative)
        if entry is not None:
            return entry
        prefix = relative.rstrip("/") + "/"
        if any(path.startswith(prefix) for path in tree):
            return ("040000", "tree", "")
        return None

    def _live_entry(self, path: Path) -> tuple[str, str, str] | None:
        if not path.exists() and not path.is_symlink():
            return None
        if path.is_symlink():
            content = os.readlink(path).encode("utf-8", errors="surrogateescape")
            return ("120000", "blob", _git_blob_sha1(content))
        if _path_is_reparse_point(path):
            raise WorkspaceRollbackConflict(
                [path.relative_to(self.workspace).as_posix()],
                "Rollback stopped because a path became a Windows reparse point.",
            )
        if path.is_dir():
            return ("040000", "tree", "")
        if path.is_file():
            mode = "100755" if path.stat(follow_symlinks=False).st_mode & stat.S_IXUSR else "100644"
            return (mode, "blob", _git_blob_sha1(path.read_bytes()))
        raise WorkspaceRollbackConflict(
            [path.relative_to(self.workspace).as_posix()],
            "Rollback stopped because a path changed to an unsupported filesystem type.",
        )

    def _assert_live_directory_matches_expected(
        self,
        relative: str,
        directory: Path,
        expected_tree: dict[str, tuple[str, str, str]] | None,
    ) -> None:
        if expected_tree is None:
            return
        unsafe: set[str] = set()
        self._collect_unprotected_directory(directory, expected_tree, unsafe)
        prefix = relative.rstrip("/") + "/"
        for expected_path in expected_tree:
            if not expected_path.startswith(prefix):
                continue
            target = self._safe_workspace_path(expected_path)
            if self._live_entry(target) != expected_tree[expected_path]:
                unsafe.add(expected_path)
        if unsafe:
            raise WorkspaceRollbackConflict(
                sorted(unsafe),
                "Rollback stopped because directory content changed while files were "
                "being restored. The late content was preserved.",
            )

    def _assert_restore_will_not_delete_unprotected(
        self,
        target_revision: str,
        current_revision: str,
        paths: list[str],
    ) -> None:
        """Preflight every destructive type transition before the first write.

        Git trees do not contain excluded files or empty directories.  A blind
        ``rmtree`` during a file/directory type rollback could therefore erase a
        later ``.env`` file, dependency directory, or another unprotected entry
        without conflict detection seeing it.  Refuse the whole rollback when a
        path that would be removed is not represented in the current safety tree.
        """

        target_tree = self._tree_entries(target_revision)
        current_tree = self._tree_entries(current_revision)
        unsafe: set[str] = set()

        for relative in paths:
            target = self._safe_workspace_path(relative)
            desired = target_tree.get(relative)

            # Restoring a child may first unlink a file/symlink that currently
            # blocks one of its parent directories.
            parent = target.parent
            while parent != self.workspace and parent != parent.parent:
                if parent.is_symlink() or parent.is_file():
                    self._collect_unprotected_leaf(parent, current_tree, unsafe)
                parent = parent.parent

            if target.is_symlink() or target.is_file():
                if desired is None:
                    self._collect_unprotected_leaf(target, current_tree, unsafe)
            elif target.exists() and target.is_dir():
                # A directory at an exact changed path is removed only when the
                # target tree wants a blob there, or wants the path absent.
                if desired is not None or relative not in target_tree:
                    self._collect_unprotected_directory(target, current_tree, unsafe)
            elif target.exists():
                unsafe.add(relative)

        if unsafe:
            paths_preview = sorted(unsafe)
            preview = ", ".join(paths_preview[:5])
            suffix = "" if len(paths_preview) <= 5 else f" and {len(paths_preview) - 5} more"
            raise WorkspaceRollbackConflict(
                paths_preview,
                "Rollback stopped because a file/directory type change would also "
                f"delete unprotected workspace content: {preview}{suffix}. Move or "
                "remove that content manually before retrying.",
            )

    def _paths_with_alternate_streams(self, paths: list[str]) -> list[str]:
        if os.name != "nt":
            return []
        affected: list[str] = []
        for relative in paths:
            target = self._safe_workspace_path(relative)
            for candidate in self._existing_paths_for_ads(target):
                streams = _windows_alternate_streams(candidate)
                if streams:
                    display = candidate.relative_to(self.workspace).as_posix()
                    affected.extend(f"{display}:{stream}" for stream in streams)
        return sorted(set(affected))

    def _existing_paths_for_ads(self, target: Path) -> list[Path]:
        if target.is_symlink() or target.is_file():
            return [target]
        if not target.exists() or not target.is_dir() or _path_is_reparse_point(target):
            return []
        # Directories can carry NTFS alternate streams too. Include every
        # traversed directory as well as ordinary files because a rollback may
        # remove or replace either one.
        result: list[Path] = [target]
        stack = [target]
        while stack:
            directory = stack.pop()
            try:
                children = list(os.scandir(directory))
            except OSError:
                continue
            for child in children:
                path = Path(child.path)
                try:
                    if child.is_symlink() or _is_reparse_point(child.stat(follow_symlinks=False)):
                        continue
                    if child.is_dir(follow_symlinks=False):
                        result.append(path)
                        stack.append(path)
                    elif child.is_file(follow_symlinks=False):
                        result.append(path)
                except OSError:
                    continue
        return result

    def _collect_unprotected_directory(
        self,
        directory: Path,
        current_tree: dict[str, tuple[str, str, str]],
        unsafe: set[str],
    ) -> None:
        relative_directory = directory.relative_to(self.workspace)
        relative_text = relative_directory.as_posix()
        if directory.is_symlink() or _path_is_reparse_point(directory):
            unsafe.add(relative_text)
            return
        if snapshot_relative_exclusion_reason(relative_directory) is not None:
            unsafe.add(relative_text)
            return
        try:
            children = list(os.scandir(directory))
        except OSError:
            unsafe.add(relative_text)
            return
        if not children:
            # Empty directories cannot be represented by Git, so ownership cannot
            # be proven.  Preserve them instead of silently deleting them.
            unsafe.add(relative_text)
            return
        for child in children:
            source = Path(child.path)
            relative = relative_directory / child.name
            relative_text = relative.as_posix()
            if snapshot_relative_exclusion_reason(relative) is not None:
                unsafe.add(relative_text)
                continue
            try:
                if child.is_symlink():
                    self._collect_unprotected_leaf(source, current_tree, unsafe)
                    continue
                file_stat = child.stat(follow_symlinks=False)
                if _is_reparse_point(file_stat):
                    unsafe.add(relative_text)
                elif child.is_dir(follow_symlinks=False):
                    try:
                        source.resolve().relative_to(self.workspace)
                    except ValueError:
                        unsafe.add(relative_text)
                        continue
                    self._collect_unprotected_directory(source, current_tree, unsafe)
                elif child.is_file(follow_symlinks=False):
                    self._collect_unprotected_leaf(source, current_tree, unsafe)
                else:
                    unsafe.add(relative_text)
            except OSError:
                unsafe.add(relative_text)

    def _collect_unprotected_leaf(
        self,
        path: Path,
        current_tree: dict[str, tuple[str, str, str]],
        unsafe: set[str],
    ) -> None:
        try:
            relative = path.relative_to(self.workspace).as_posix()
        except ValueError:
            unsafe.add(str(path))
            return
        entry = current_tree.get(relative)
        if entry is None:
            unsafe.add(relative)
            return
        mode = entry[0]
        if path.is_symlink():
            if mode != "120000":
                unsafe.add(relative)
        elif mode == "120000":
            unsafe.add(relative)

    def _remove_parent_blockers(self, path: Path) -> None:
        parents: list[Path] = []
        current = path.parent
        while current != self.workspace and current != current.parent:
            parents.append(current)
            current = current.parent
        for parent in reversed(parents):
            if parent.is_symlink() or parent.is_file():
                parent.unlink()
            elif parent.exists() and not parent.is_dir():
                raise WorkspaceProtectionError(
                    f"Refusing to replace unsupported parent path during rollback: {parent}"
                )

    def _tree_entries(self, revision: str) -> dict[str, tuple[str, str, str]]:
        output = self._run_git_bytes("ls-tree", "-r", "-z", revision, "--")
        result: dict[str, tuple[str, str, str]] = {}
        for record in output.split(b"\0"):
            if not record or b"\t" not in record:
                continue
            metadata, path_raw = record.split(b"\t", 1)
            parts = metadata.decode("ascii").split()
            if len(parts) != 3:
                continue
            path = path_raw.decode("utf-8", errors="surrogateescape").replace("\\", "/")
            result[path] = (parts[0], parts[1], parts[2])
        return result

    def _safe_workspace_path(self, relative: str) -> Path:
        relative_path = Path(*relative.split("/"))
        if (
            not relative
            or relative_path.is_absolute()
            or any(part in {"", ".", ".."} for part in relative_path.parts)
        ):
            raise WorkspaceProtectionError(f"Unsafe snapshot path: {relative!r}")
        candidate = self.workspace / relative_path
        # Never perform a destructive operation through a symlink, junction, or
        # other reparse-point parent, even when it resolves to another location
        # inside the workspace.  Re-check this for every mutation to narrow the
        # external editor/process race window.
        current_parent = self.workspace
        for part in relative_path.parts[:-1]:
            current_parent /= part
            if current_parent.is_symlink() or _path_is_reparse_point(current_parent):
                raise WorkspaceProtectionError(
                    f"Snapshot path uses an aliased parent directory: {relative}"
                )
        resolved_parent = candidate.parent.resolve()
        try:
            resolved_parent.relative_to(self.workspace)
        except ValueError as exc:
            raise WorkspaceProtectionError(
                f"Snapshot path escapes through a symlink: {relative}"
            ) from exc
        return candidate

    def _delete_restored_path(self, path: Path) -> None:
        self._assert_no_aliasing_parent(path)
        if path.is_symlink() or path.is_file():
            path.unlink()
        elif path.exists() and path.is_dir():
            if _path_is_reparse_point(path):
                raise WorkspaceProtectionError(
                    f"Refusing to recursively remove a reparse point: {path}"
                )
            shutil.rmtree(path)
        elif path.exists():
            raise WorkspaceProtectionError(f"Refusing to remove unsupported path: {path}")
        # Do not prune now-empty parent directories. Git does not represent
        # directory ownership or NTFS directory ADS, so even an apparently empty
        # folder may contain user metadata created after the task. Preserving a
        # harmless empty directory is safer than deleting untracked state.

    def _assert_no_aliasing_parent(self, path: Path) -> None:
        try:
            relative = path.relative_to(self.workspace)
        except ValueError as exc:
            raise WorkspaceProtectionError(f"Path is outside the workspace: {path}") from exc
        parent = self.workspace
        for part in relative.parts[:-1]:
            parent /= part
            if parent.is_symlink() or _path_is_reparse_point(parent):
                raise WorkspaceRollbackConflict(
                    [relative.as_posix()],
                    "Rollback stopped because a parent directory became a symlink or "
                    "Windows reparse point. No operation was performed through it.",
                )

    @staticmethod
    def _atomic_write(path: Path, content: bytes, *, executable: bool) -> None:
        if path.exists() and path.is_dir() and not path.is_symlink():
            raise WorkspaceProtectionError(f"Refusing to replace a directory: {path}")
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            if os.name != "nt":
                temporary.chmod(0o755 if executable else 0o644)
            os.replace(temporary, path)
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass

    @staticmethod
    def _replace_symlink(path: Path, content: bytes) -> None:
        if path.is_symlink() or path.is_file():
            path.unlink()
        elif path.exists():
            raise WorkspaceProtectionError(f"Refusing to replace a directory: {path}")
        path.parent.mkdir(parents=True, exist_ok=True)
        target = content.decode("utf-8", errors="strict")
        try:
            path.symlink_to(target)
        except OSError as exc:
            raise WorkspaceProtectionError(f"Could not restore symlink {path}: {exc}") from exc

    def _path_intersection(self, expected: list[str], actual: list[str]) -> list[str]:
        return sorted(
            path
            for path in expected
            if any(self._paths_overlap(path, changed) for changed in actual)
        )

    def _paths_overlap(self, left: str, right: str) -> bool:
        left_key = self._path_key(left).strip("/")
        right_key = self._path_key(right).strip("/")
        return (
            left_key == right_key
            or left_key.startswith(right_key + "/")
            or right_key.startswith(left_key + "/")
        )

    @staticmethod
    def _path_key(path: str) -> str:
        normalized = path.replace("\\", "/")
        return normalized.casefold() if os.name == "nt" else normalized

    def _public_record(self, record: dict[str, Any]) -> dict[str, Any]:
        concurrent_task_ids = [
            str(value)
            for value in record.get("concurrent_task_ids") or []
            if value
        ]
        return {
            "snapshot_id": str(record.get("snapshot_id") or ""),
            "task_id": str(record.get("task_id") or ""),
            "session_id": str(record.get("session_id") or ""),
            "backend": str(record.get("backend") or "side-git"),
            "protected": bool(record.get("protected")),
            "status": str(record.get("status") or "unknown"),
            "has_changes": bool(record.get("has_changes")),
            "changed_files": list(record.get("changed_files") or []),
            "additions": int(record.get("additions") or 0),
            "deletions": int(record.get("deletions") or 0),
            "diff_available": bool(
                record.get("has_changes")
                and record.get("after_revision")
                and not concurrent_task_ids
            ),
            "rollback_available": bool(record.get("rollback_available")),
            "rollback_block_reason": str(record.get("rollback_block_reason") or ""),
            "change_attribution": str(record.get("change_attribution") or "task"),
            "concurrent_task_ids": concurrent_task_ids,
            "rolled_back": bool(record.get("rolled_back")),
            "rollback_state": str(record.get("rollback_state") or "idle"),
            "rollback_recovery_event_pending": bool(
                record.get("rollback_recovery_event_pending")
            ),
            "error": str(record.get("error") or record.get("rollback_error") or ""),
            "created_at": record.get("created_at"),
            "completed_at": record.get("completed_at"),
            "rolled_back_at": record.get("rolled_back_at"),
        }

    def _unprotected(
        self,
        task_id: str,
        session_id: str,
        reason: str | None = None,
    ) -> dict[str, Any]:
        return {
            "snapshot_id": "",
            "task_id": task_id,
            "session_id": session_id,
            "backend": "side-git",
            "protected": False,
            "status": "unavailable",
            "has_changes": False,
            "changed_files": [],
            "additions": 0,
            "deletions": 0,
            "diff_available": False,
            "rollback_available": False,
            "rolled_back": False,
            "error": reason or self._unavailable_reason or "Workspace protection is unavailable.",
        }

    def _record_path(self, task_id: str) -> Path:
        return self.records_dir / f"{_task_token(task_id)}.json"

    def _active_records(self, *, exclude_task_id: str = "") -> list[dict[str, Any]]:
        active: list[dict[str, Any]] = []
        for path in self.records_dir.glob("*.json"):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError, TypeError, ValueError):
                continue
            if not isinstance(payload, dict) or payload.get("status") != "active":
                continue
            if str(payload.get("task_id") or "") == exclude_task_id:
                continue
            active.append(payload)
        return active

    def _load_record(self, task_id: str) -> dict[str, Any] | None:
        path = self._record_path(task_id)
        if not path.exists():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            return None
        return payload if isinstance(payload, dict) else None

    def _write_record(self, record: dict[str, Any]) -> None:
        path = self._record_path(str(record["task_id"]))
        temporary = path.with_suffix(path.suffix + ".tmp")
        with temporary.open("w", encoding="utf-8", newline="\n") as stream:
            json.dump(record, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)

    def _prune_records(self) -> None:
        records: list[tuple[Path, dict[str, Any]]] = []
        for path in self.records_dir.glob("*.json"):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError, TypeError, ValueError):
                continue
            if isinstance(payload, dict) and payload.get("status") != "active":
                records.append((path, payload))
        records.sort(key=lambda item: str(item[1].get("updated_at") or ""), reverse=True)
        pruned = False
        for path, record in records[MAX_TASK_RECORDS:]:
            token = _task_token(str(record.get("task_id") or path.stem))
            for suffix in (
                "before",
                "after",
                "rollback-safety",
                "rollback-result",
                "rollback-recovered",
                "rollback-emergency",
                "rollback-cas",
            ):
                try:
                    self._run_git("update-ref", "-d", f"refs/stellarcode/tasks/{token}/{suffix}")
                except WorkspaceProtectionError:
                    pass
            try:
                path.unlink()
                pruned = True
            except FileNotFoundError:
                pass
        if pruned:
            # Deleting refs/records alone leaves their blobs unreachable in the
            # bare repository forever. Reclaim them when retention actually
            # evicts history; normal task finalization does not pay this cost.
            try:
                self._run_git("reflog", "expire", "--expire=now", "--all")
                self._run_git("gc", "--prune=now")
            except WorkspaceProtectionError:
                # Retention metadata is already correct. A later prune/startup can
                # retry object cleanup without affecting rollback correctness.
                pass

    def _git_environment(self) -> dict[str, str]:
        environment = self._plain_git_environment()
        environment.update(
            GIT_DIR=str(self.repository_dir),
            GIT_WORK_TREE=str(self.workspace),
            GIT_INDEX_FILE=str(self.index_file),
            GIT_AUTHOR_NAME="StellarCode",
            GIT_AUTHOR_EMAIL="snapshot@stellarcode.local",
            GIT_COMMITTER_NAME="StellarCode",
            GIT_COMMITTER_EMAIL="snapshot@stellarcode.local",
            GIT_OPTIONAL_LOCKS="0",
        )
        return environment

    @staticmethod
    def _plain_git_environment() -> dict[str, str]:
        # A desktop process can inherit repository-routing variables from the
        # terminal or parent IDE that launched Tauri.  None of them may influence
        # StellarCode's isolated Side-Git object database.
        environment = {
            key: value
            for key, value in os.environ.items()
            if not key.upper().startswith("GIT_")
        }
        environment.update(
            GIT_TERMINAL_PROMPT="0",
            GIT_PAGER="cat",
            GIT_CONFIG_NOSYSTEM="1",
            GIT_CONFIG_GLOBAL=os.devnull,
            GCM_INTERACTIVE="Never",
        )
        return environment

    def _run_plain_git(self, *args: str) -> str:
        return self._run_process(args, environment=self._plain_git_environment()).decode(
            "utf-8", errors="replace"
        )

    def _run_git(
        self,
        *args: str,
        input_text: str | None = None,
        timeout_seconds: float = 90,
    ) -> str:
        input_bytes = input_text.encode("utf-8") if input_text is not None else None
        return self._run_process(
            args,
            environment=self._git_environment(),
            input_bytes=input_bytes,
            timeout_seconds=timeout_seconds,
        ).decode("utf-8", errors="replace")

    def _run_git_bytes(self, *args: str) -> bytes:
        return self._run_process(args, environment=self._git_environment())

    def _run_process(
        self,
        args: tuple[str, ...],
        *,
        environment: dict[str, str],
        input_bytes: bytes | None = None,
        timeout_seconds: float = 90,
    ) -> bytes:
        if self.git is None:
            raise WorkspaceProtectionError("Git executable was not found.")
        creation_flags = 0
        if os.name == "nt":
            creation_flags = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
        try:
            process = subprocess.Popen(
                [self.git, *args],
                cwd=self.workspace,
                env=environment,
                stdin=subprocess.PIPE if input_bytes is not None else subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                creationflags=creation_flags,
                start_new_session=os.name != "nt",
            )
        except OSError as exc:
            raise WorkspaceProtectionError(f"Git snapshot command failed: {exc}") from exc
        try:
            stdout, stderr = process.communicate(input=input_bytes, timeout=timeout_seconds)
        except subprocess.TimeoutExpired as exc:
            self._terminate_process_tree(process)
            try:
                process.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.communicate()
            command = " ".join(args[:2])
            raise WorkspaceProtectionError(
                f"Git snapshot command timed out after {timeout_seconds:g} seconds: {command}"
            ) from exc
        except (OSError, subprocess.SubprocessError) as exc:
            self._terminate_process_tree(process)
            raise WorkspaceProtectionError(f"Git snapshot command failed: {exc}") from exc
        if process.returncode != 0:
            error = stderr.decode("utf-8", errors="replace").strip()
            raise WorkspaceProtectionError(
                f"Git snapshot command failed ({' '.join(args[:2])}): {error}"
            )
        return stdout

    @staticmethod
    def _terminate_process_tree(process: subprocess.Popen[bytes]) -> None:
        if process.poll() is not None:
            return
        if os.name == "nt":
            system_root = Path(os.environ.get("SystemRoot") or r"C:\Windows")
            taskkill = system_root / "System32" / "taskkill.exe"
            try:
                subprocess.run(
                    [str(taskkill), "/PID", str(process.pid), "/T", "/F"],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=5,
                    check=False,
                    creationflags=subprocess.CREATE_NO_WINDOW,
                )
                return
            except (OSError, subprocess.SubprocessError):
                pass
        else:
            try:
                os.killpg(process.pid, signal.SIGKILL)
                return
            except (OSError, ProcessLookupError):
                pass
        try:
            process.kill()
        except OSError:
            pass


def _task_token(task_id: str) -> str:
    normalized = re.sub(r"[^A-Za-z0-9._-]+", "-", task_id).strip("-.")
    digest = hashlib.sha256(task_id.encode("utf-8")).hexdigest()[:12]
    return f"{normalized[:48] or 'task'}-{digest}"


def _git_blob_sha1(content: bytes) -> str:
    header = f"blob {len(content)}\0".encode("ascii")
    return hashlib.sha1(header + content, usedforsecurity=False).hexdigest()


def _is_reparse_point(file_stat: os.stat_result) -> bool:
    """Return true for Windows junctions and other non-symlink reparse entries."""

    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    attributes = int(getattr(file_stat, "st_file_attributes", 0) or 0)
    return os.name == "nt" and bool(attributes & reparse_flag)


def _path_is_reparse_point(path: Path) -> bool:
    try:
        return _is_reparse_point(path.lstat())
    except OSError:
        return False


def _windows_alternate_streams(path: Path) -> list[str]:
    """Enumerate named NTFS data streams without invoking PowerShell."""

    if os.name != "nt" or not path.exists():
        return []
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    find_first = kernel32.FindFirstStreamW
    find_first.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.LPVOID, wintypes.DWORD]
    find_first.restype = wintypes.HANDLE
    find_next = kernel32.FindNextStreamW
    find_next.argtypes = [wintypes.HANDLE, wintypes.LPVOID]
    find_next.restype = wintypes.BOOL
    find_close = kernel32.FindClose
    find_close.argtypes = [wintypes.HANDLE]
    find_close.restype = wintypes.BOOL

    class _Win32FindStreamData(ctypes.Structure):
        _fields_ = [
            ("stream_size", ctypes.c_longlong),
            ("stream_name", wintypes.WCHAR * (260 + 36)),
        ]

    data = _Win32FindStreamData()
    handle = find_first(str(path), 0, ctypes.byref(data), 0)
    invalid = wintypes.HANDLE(-1).value
    if handle == invalid:
        error = ctypes.get_last_error()
        if error in {2, 3, 38}:  # not found/path not found/end of stream list
            return []
        raise WorkspaceProtectionError(
            f"Unable to verify NTFS alternate data streams for {path} (WinError {error})."
        )
    streams: list[str] = []
    try:
        while True:
            name = str(data.stream_name)
            if name and name != "::$DATA":
                streams.append(name.removeprefix(":").removesuffix(":$DATA"))
            if not find_next(handle, ctypes.byref(data)):
                break
    finally:
        find_close(handle)
    return sorted(set(streams))


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
