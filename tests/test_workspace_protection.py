from __future__ import annotations

import os
import shutil
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path

import pytest

from stellarcode.hitl import ApprovalRequest, ApprovalResult
from stellarcode.runtime.hitl import RuntimeHitlHandler
from stellarcode.protection import (
    WorkspaceProtectionError,
    WorkspaceProtectionService,
    WorkspaceRollbackConflict,
    preview_delete_file,
    preview_write_file,
)
from stellarcode.protection.workspace import _task_token
from stellarcode.tools import ToolExecutionError, build_default_registry


def _service(workspace: Path, storage: Path) -> WorkspaceProtectionService:
    service = WorkspaceProtectionService(workspace, storage)
    if not service.available:
        pytest.skip("Git-backed workspace protection is unavailable in this environment")
    return service


def test_isolated_task_worktree_keeps_changes_private_until_finalize(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / "app.py"
    target.write_text("value = 1\n", encoding="utf-8")
    service = _service(workspace, tmp_path / "protection")

    started = service.begin_task("task-isolated", "session-one", isolated=True)
    worktree = service.task_workspace("task-isolated", "session-one")
    assert worktree is not None
    (worktree / "app.py").write_text("value = 2\n", encoding="utf-8")

    assert started["worktree_isolated"] is True
    assert target.read_text(encoding="utf-8") == "value = 1\n"
    finalized = service.finalize_task("task-isolated", "completed")

    assert finalized["merge_state"] == "merged"
    assert finalized["has_changes"] is True
    assert finalized["rollback_available"] is True
    assert finalized["worktree_path"] == ""
    assert target.read_text(encoding="utf-8") == "value = 2\n"


def test_isolated_worktree_uses_short_git_paths_in_deep_runtime_storage(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "app.py").write_text("value = 1\n", encoding="utf-8")
    storage = tmp_path
    depth = 0
    while len(str(storage / "workspace-protection")) < 150:
        storage /= f"runtime-project-storage-{depth:02d}"
        depth += 1
    storage /= "workspace-protection"
    service = _service(workspace, storage)
    task_id = "task-" + "0123456789abcdef" * 3

    started = service.begin_task(task_id, "session-one", isolated=True)
    worktree = service.task_workspace(task_id, "session-one")

    assert worktree is not None
    assert len(worktree.name) == 16
    pointer_value = (worktree / ".git").read_text(encoding="utf-8").removeprefix(
        "gitdir: "
    ).strip()
    if os.name == "nt":
        assert len(pointer_value) < 220
    (worktree / "app.py").write_text("value = 2\n", encoding="utf-8")
    finalized = service.finalize_task(task_id, "completed")
    assert started["worktree_isolated"] is True
    assert finalized["merge_state"] == "merged", finalized["error"]
    assert (workspace / "app.py").read_text(encoding="utf-8") == "value = 2\n"


def test_isolated_task_can_store_its_worktree_under_a_custom_root(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "app.py").write_text("before\n", encoding="utf-8")
    storage = tmp_path / "runtime" / "workspace-protection"
    custom_root = tmp_path / "custom-worktrees" / "projects" / "project-one" / "w"
    service = WorkspaceProtectionService(
        workspace,
        storage,
        worktree_root=custom_root,
    )
    if not service.available:
        pytest.skip("Git-backed workspace protection is unavailable in this environment")

    started = service.begin_task("task-custom-root", "session-one", isolated=True)
    worktree = Path(started["worktree_path"])

    assert worktree.parent == custom_root.resolve()
    assert not (storage / "w").exists()
    (worktree / "app.py").write_text("after\n", encoding="utf-8")
    finalized = service.finalize_task("task-custom-root", "completed")
    assert finalized["merge_state"] == "merged"
    assert (workspace / "app.py").read_text(encoding="utf-8") == "after\n"
    assert not worktree.exists()


def test_custom_worktree_root_cannot_be_inside_the_project(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    with pytest.raises(WorkspaceProtectionError, match="separate from the project"):
        WorkspaceProtectionService(
            workspace,
            tmp_path / "runtime" / "workspace-protection",
            worktree_root=workspace / ".stellarcode-worktrees",
        )


def test_startup_removes_an_unreferenced_legacy_task_repository(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    storage = tmp_path / "protection"
    service = _service(workspace, storage)
    service.close()
    legacy_root = storage / "task-repositories"
    orphan = legacy_root / f"{_task_token('task-' + 'a' * 48)}.git"
    object_dir = orphan / "objects" / "aa"
    object_dir.mkdir(parents=True)
    object_file = object_dir / ("b" * 38)
    object_file.write_bytes(b"orphaned failed worktree data")
    if os.name == "nt":
        os.chmod(object_file, 0o444)

    restarted = _service(workspace, storage)

    assert restarted.available is True
    assert not orphan.exists()


def test_startup_retries_cleanup_for_an_already_merged_worktree(
    monkeypatch,
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "app.py").write_text("before\n", encoding="utf-8")
    storage = tmp_path / "protection"
    service = _service(workspace, storage)
    started = service.begin_task("task-cleanup-retry", "session-one", isolated=True)
    worktree = Path(started["worktree_path"])
    repository = storage / "r" / f"{worktree.name}.git"
    (worktree / "app.py").write_text("after\n", encoding="utf-8")

    monkeypatch.setattr(service, "_remove_task_worktree", lambda _record: None)
    finalized = service.finalize_task("task-cleanup-retry", "completed")
    assert finalized["merge_state"] == "merged"
    assert worktree.exists()
    assert repository.exists()
    service.close()

    restarted = _service(workspace, storage)

    assert restarted.available is True
    assert not worktree.exists()
    assert not repository.exists()


def test_isolated_concurrent_tasks_merge_disjoint_changes_with_exact_attribution(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "one.txt").write_text("one old\n", encoding="utf-8")
    (workspace / "two.txt").write_text("two old\n", encoding="utf-8")
    service = _service(workspace, tmp_path / "protection")
    service.begin_task("task-one", "session-one", isolated=True)
    service.begin_task("task-two", "session-two", isolated=True)
    first = service.task_workspace("task-one", "session-one")
    second = service.task_workspace("task-two", "session-two")
    assert first is not None and second is not None
    (first / "one.txt").write_text("one new\n", encoding="utf-8")
    (second / "two.txt").write_text("two new\n", encoding="utf-8")

    first_result = service.finalize_task("task-one", "completed")
    second_result = service.finalize_task("task-two", "completed")

    assert [item["path"] for item in first_result["changed_files"]] == ["one.txt"]
    assert [item["path"] for item in second_result["changed_files"]] == ["two.txt"]
    assert first_result["concurrent_task_ids"] == []
    assert second_result["concurrent_task_ids"] == []
    assert first_result["rollback_available"] is True
    assert second_result["rollback_available"] is True
    assert (workspace / "one.txt").read_text(encoding="utf-8") == "one new\n"
    assert (workspace / "two.txt").read_text(encoding="utf-8") == "two new\n"


def test_isolated_task_merge_conflict_preserves_project_and_retains_worktree(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / "shared.txt"
    target.write_text("shared baseline\n", encoding="utf-8")
    service = _service(workspace, tmp_path / "protection")
    service.begin_task("task-a", "session-a", isolated=True)
    service.begin_task("task-b", "session-b", isolated=True)
    task_a = service.task_workspace("task-a", "session-a")
    task_b = service.task_workspace("task-b", "session-b")
    assert task_a is not None and task_b is not None
    (task_a / "shared.txt").write_text("result from a\n", encoding="utf-8")
    (task_b / "shared.txt").write_text("result from b\n", encoding="utf-8")

    service.finalize_task("task-a", "completed")
    conflict = service.finalize_task("task-b", "completed")

    assert conflict["merge_conflict"] is True
    assert conflict["status"] == "failed"
    assert conflict["rollback_available"] is False
    assert conflict["worktree_path"]
    assert Path(conflict["worktree_path"]).is_dir()
    assert target.read_text(encoding="utf-8") == "result from a\n"


def test_isolated_task_worktree_survives_runtime_restart(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / "resume.txt"
    target.write_text("baseline\n", encoding="utf-8")
    storage = tmp_path / "protection"
    first_service = _service(workspace, storage)
    started = first_service.begin_task("task-resume", "session-one", isolated=True)
    worktree = first_service.task_workspace("task-resume", "session-one")
    assert worktree is not None
    (worktree / "resume.txt").write_text("changed before crash\n", encoding="utf-8")
    first_service.close()

    restarted = _service(workspace, storage)
    recovered = restarted.validate_recovery_baseline(
        "task-resume",
        "session-one",
        started["snapshot_id"],
    )
    recovered_worktree = restarted.task_workspace("task-resume", "session-one")

    assert recovered["worktree_isolated"] is True
    assert recovered_worktree == worktree
    assert (recovered_worktree / "resume.txt").read_text(encoding="utf-8") == (
        "changed before crash\n"
    )
    finalized = restarted.finalize_task("task-resume", "completed")
    assert finalized["merge_state"] == "merged"
    assert target.read_text(encoding="utf-8") == "changed before crash\n"


def test_isolated_tasks_use_private_git_repositories(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / "app.py"
    target.write_text("value = 1\n", encoding="utf-8")
    service = _service(workspace, tmp_path / "protection")
    service.begin_task("task-private-a", "session-a", isolated=True)
    service.begin_task("task-private-b", "session-b", isolated=True)
    first = service.task_workspace("task-private-a", "session-a")
    assert first is not None
    first_record = service._load_record("task-private-a")
    second_record = service._load_record("task-private-b")
    assert first_record is not None and second_record is not None
    first_repository = Path(str(first_record["worktree_repository_path"])).resolve()
    second_repository = Path(str(second_record["worktree_repository_path"])).resolve()
    assert first_repository != second_repository
    assert first_repository != service.repository_dir.resolve()
    assert second_repository != service.repository_dir.resolve()

    target_ref = f"refs/stellarcode/tasks/{_task_token('task-private-b')}/before"
    shared_target = service._existing_snapshot_ref(target_ref)
    (first / "app.py").write_text("value = 2\n", encoding="utf-8")
    _git(first, "add", "app.py")
    _git(
        first,
        "-c",
        "user.name=Task Agent",
        "-c",
        "user.email=task@stellarcode.local",
        "commit",
        "-m",
        "task-local commit",
    )
    # A task may freely use Git, including creating Side-Git-looking refs.  Its
    # repository is private, so this must not alter another task's baseline.
    _git(first, "update-ref", target_ref, "HEAD")

    assert service._existing_snapshot_ref(target_ref) == shared_target
    finalized = service.finalize_task("task-private-a", "completed")
    assert finalized["merge_state"] == "merged"
    assert target.read_text(encoding="utf-8") == "value = 2\n"


def test_terminal_merge_rejects_a_rewritten_worktree_git_pointer(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / "app.py"
    target.write_text("value = 1\n", encoding="utf-8")
    service = _service(workspace, tmp_path / "protection")
    service.begin_task("task-git-pointer", "session-one", isolated=True)
    worktree = service.task_workspace("task-git-pointer", "session-one")
    assert worktree is not None
    (worktree / "app.py").write_text("value = 2\n", encoding="utf-8")
    pointer = worktree / ".git"
    original_pointer = pointer.read_text(encoding="utf-8")
    if os.name == "nt":
        attrib = Path(os.environ.get("SystemRoot") or r"C:\Windows") / "System32" / "attrib.exe"
        subprocess.run([str(attrib), "-H", str(pointer)], check=True)
    os.chmod(pointer, 0o666)
    pointer.write_text(f"gitdir: {service.repository_dir}\n", encoding="utf-8")

    result = service.finalize_task("task-git-pointer", "completed")

    assert result["merge_conflict"] is True
    assert result["status"] == "failed"
    assert "untrusted" in result["error"]
    assert target.read_text(encoding="utf-8") == "value = 1\n"

    # Restore the pointer only to clean up the retained forensic worktree in
    # this test; production intentionally leaves a conflict worktree intact.
    pointer.write_text(original_pointer, encoding="utf-8")
    record = service._load_record("task-git-pointer")
    assert record is not None
    service._remove_task_worktree(record)


def test_isolated_post_snapshot_excludes_edits_arriving_during_finalization(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / "app.py"
    target.write_text("value = 1\n", encoding="utf-8")
    unrelated = workspace / "editor-note.txt"
    service = _service(workspace, tmp_path / "protection")
    service.begin_task("task-finalize-window", "session-one", isolated=True)
    worktree = service.task_workspace("task-finalize-window", "session-one")
    assert worktree is not None
    (worktree / "app.py").write_text("value = 2\n", encoding="utf-8")
    original_capture = service._capture_commit

    def edit_after_pre_merge_snapshot(message: str, ref: str) -> str:
        revision = original_capture(message, ref)
        if "pre-merge baseline" in message:
            unrelated.write_text("arrived during finalization\n", encoding="utf-8")
        return revision

    monkeypatch.setattr(service, "_capture_commit", edit_after_pre_merge_snapshot)

    finalized = service.finalize_task("task-finalize-window", "completed")

    assert [item["path"] for item in finalized["changed_files"]] == ["app.py"]
    assert target.read_text(encoding="utf-8") == "value = 2\n"
    assert unrelated.read_text(encoding="utf-8") == "arrived during finalization\n"
    service.rollback_task("task-finalize-window", "session-one")
    assert target.read_text(encoding="utf-8") == "value = 1\n"
    assert unrelated.read_text(encoding="utf-8") == "arrived during finalization\n"


def _git(repository: Path, *arguments: str) -> bytes:
    executable = shutil.which("git")
    if executable is None:
        pytest.skip("Git is unavailable")
    completed = subprocess.run(
        [executable, *arguments],
        cwd=repository,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr.decode("utf-8", errors="replace")
    return completed.stdout


def test_file_change_previews_cover_create_modify_and_delete(tmp_path: Path) -> None:
    created = preview_write_file(tmp_path, "new.py", "first\nsecond\n")

    assert created["operation"] == "create"
    assert created["path"] == "new.py"
    assert created["workspace_scoped"] is True
    assert created["additions"] == 2
    assert created["deletions"] == 0
    assert created["before_sha256"] is None
    assert created["after_sha256"]
    assert "--- /dev/null" in created["diff"]
    assert "+++ b/new.py" in created["diff"]

    target = tmp_path / "existing.py"
    target.write_text("old line\nkeep\n", encoding="utf-8")
    modified = preview_write_file(tmp_path, "existing.py", "new line\nkeep\n")

    assert modified["operation"] == "modify"
    assert modified["additions"] == 1
    assert modified["deletions"] == 1
    assert "-old line" in modified["diff"]
    assert "+new line" in modified["diff"]
    assert modified["before_sha256"] != modified["after_sha256"]

    deleted = preview_delete_file(tmp_path, "existing.py")

    assert deleted["operation"] == "delete"
    assert deleted["additions"] == 0
    assert deleted["deletions"] == 2
    assert deleted["before_sha256"] == modified["before_sha256"]
    assert deleted["after_sha256"] is None
    assert "+++ /dev/null" in deleted["diff"]


def test_sensitive_file_preview_and_tool_event_arguments_hide_contents(tmp_path: Path) -> None:
    secret = "GLM_API_KEY=secret-value-that-must-not-leak"
    preview = preview_write_file(tmp_path, ".env.production", secret)

    assert preview["sensitive"] is True
    assert preview["rollback_protected"] is False
    assert preview["protection_reason"] == "sensitive_path"
    assert secret not in preview["diff"]
    assert preview["diff"] == "[Diff hidden because this path may contain secrets.]"

    registry = build_default_registry(tmp_path)
    event_arguments = registry.event_arguments(
        "write_file",
        {"path": ".env.production", "content": secret},
    )

    assert secret not in str(event_arguments)
    assert event_arguments["path"] == ".env.production"
    assert event_arguments["content"] == f"[full content omitted: {len(secret)} characters]"

    patch_arguments = registry.event_arguments(
        "apply_patch",
        {
            "path": ".env.production",
            "edits": [{"old_text": secret, "new_text": "TOKEN=replaced"}],
        },
    )
    assert secret not in str(patch_arguments)
    assert patch_arguments["path"] == ".env.production"
    assert patch_arguments["edits"] == {
        "edit_count": 1,
        "old_chars": len(secret),
        "new_chars": len("TOKEN=replaced"),
        "replace_all_count": 0,
        "content": "[exact patch text omitted]",
    }


def test_overlapping_conversation_tasks_disable_ambiguous_task_rollback(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / "shared.txt"
    target.write_text("before\n", encoding="utf-8")
    service = _service(workspace, tmp_path / "protection")
    try:
        first = service.begin_task("task-one", "session-one")
        target.write_text("from task one\n", encoding="utf-8")
        second = service.begin_task("task-two", "session-two")
        target.write_text("from task two\n", encoding="utf-8")

        first_done = service.finalize_task("task-one", "completed")
        second_done = service.finalize_task("task-two", "completed")

        assert first["protected"] is True
        assert second["concurrent_task_ids"] == ["task-one"]
        assert first_done["concurrent_task_ids"] == ["task-two"]
        assert first_done["change_attribution"] == "shared_workspace_overlap"
        assert second_done["change_attribution"] == "shared_workspace_overlap"
        assert first_done["rollback_available"] is False
        assert second_done["rollback_available"] is False
        assert first_done["diff_available"] is False
        assert "another conversation task" in first_done["rollback_block_reason"]
        with pytest.raises(WorkspaceProtectionError, match="another conversation task"):
            service.rollback_task("task-one", "session-one")
    finally:
        service.close()


def test_runtime_approval_event_never_contains_full_write_content(tmp_path: Path) -> None:
    secret = "GLM_API_KEY=approval-event-secret"
    events: list[tuple[str, dict]] = []

    class _ImmediateApproval:
        def is_enabled(self) -> bool:
            return True

        def request_approval(self, request: ApprovalRequest) -> ApprovalResult:
            runtime_handler = RuntimeHitlHandler(
                lambda event_type, data: events.append((event_type, data))
            )
            pending = threading.Thread(
                target=runtime_handler.request_approval,
                args=(request,),
                daemon=True,
            )
            pending.start()
            while not events:
                pending.join(0.01)
            approval_id = str(events[0][1]["approval_id"])
            assert runtime_handler.resolve(approval_id, "reject")
            pending.join(1)
            return ApprovalResult.rejected()

    registry = build_default_registry(tmp_path, hitl_handler=_ImmediateApproval())
    registry.execute("write_file", {"path": ".env", "content": secret})

    serialized = str(events)
    assert secret not in serialized
    assert "full content omitted" in serialized


def test_task_snapshots_report_created_modified_and_deleted_files(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "modify.txt").write_text("before\n", encoding="utf-8")
    (workspace / "delete.txt").write_text("remove me\n", encoding="utf-8")
    service = _service(workspace, tmp_path / "protection")

    baseline = service.begin_task("task-diff", "session-one")
    assert baseline["protected"] is True
    assert baseline["status"] == "active"

    (workspace / "modify.txt").write_text("after\n", encoding="utf-8")
    (workspace / "create.txt").write_text("one\ntwo\n", encoding="utf-8")
    (workspace / "delete.txt").unlink()
    completed = service.finalize_task("task-diff", "completed")

    assert completed["status"] == "completed"
    assert completed["has_changes"] is True
    assert completed["rollback_available"] is True
    assert completed["diff_available"] is True
    by_path = {item["path"]: item for item in completed["changed_files"]}
    assert {path: item["status"] for path, item in by_path.items()} == {
        "create.txt": "created",
        "delete.txt": "deleted",
        "modify.txt": "modified",
    }
    assert completed["additions"] == 3
    assert completed["deletions"] == 2

    task_diff = service.task_diff("task-diff", "session-one")
    assert task_diff["diff_truncated"] is False
    assert "create.txt" in task_diff["diff"]
    assert "delete.txt" in task_diff["diff"]
    assert "modify.txt" in task_diff["diff"]


def test_task_rollback_restores_created_modified_and_deleted_files(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    modified = workspace / "modified.txt"
    deleted = workspace / "deleted.txt"
    created = workspace / "created.txt"
    modified.write_text("original modified\n", encoding="utf-8")
    deleted.write_text("original deleted\n", encoding="utf-8")
    service = _service(workspace, tmp_path / "protection")

    baseline = service.begin_task("task-rollback", "session-one")
    modified.write_text("task modified\n", encoding="utf-8")
    deleted.unlink()
    created.write_text("task created\n", encoding="utf-8")
    service.finalize_task("task-rollback", "completed")

    result = service.rollback_task(
        "task-rollback",
        "session-one",
        snapshot_id=baseline["snapshot_id"],
    )

    assert result["rolled_back"] is True
    assert result["rollback_available"] is False
    assert modified.read_text(encoding="utf-8") == "original modified\n"
    assert deleted.read_text(encoding="utf-8") == "original deleted\n"
    assert not created.exists()
    assert set(result["restored_files"]) == {
        "created.txt",
        "deleted.txt",
        "modified.txt",
    }


def test_rollback_preserves_an_empty_parent_after_deleting_a_task_file(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    generated = workspace / "generated"
    service = _service(workspace, tmp_path / "protection")

    service.begin_task("task-empty-parent", "session-one")
    generated.mkdir()
    (generated / "item.txt").write_text("created by task\n", encoding="utf-8")
    service.finalize_task("task-empty-parent", "completed")

    service.rollback_task("task-empty-parent", "session-one")

    assert not (generated / "item.txt").exists()
    # Git does not represent directory ownership or NTFS directory ADS. Keep a
    # harmless empty parent instead of risking deletion of untracked metadata.
    assert generated.is_dir()


def test_finalize_retry_reuses_the_first_durable_post_task_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / "target.txt"
    target.write_text("baseline\n", encoding="utf-8")
    service = _service(workspace, tmp_path / "protection")
    service.begin_task("task-finalize-retry", "session-one")
    target.write_text("task result\n", encoding="utf-8")

    original_diff = service._diff_details
    captured_after: list[str] = []

    def fail_after_snapshot(before: str, after: str) -> dict:
        captured_after.append(after)
        raise OSError("simulated record write boundary")

    monkeypatch.setattr(service, "_diff_details", fail_after_snapshot)
    with pytest.raises(OSError, match="record write boundary"):
        service.finalize_task("task-finalize-retry", "completed")

    target.write_text("external edit after crash\n", encoding="utf-8")
    monkeypatch.setattr(service, "_diff_details", original_diff)
    finalized = service.finalize_task("task-finalize-retry", "completed")
    diff = service.task_diff("task-finalize-retry", "session-one")["diff"]
    durable_record = service._load_record("task-finalize-retry")

    assert durable_record is not None
    assert durable_record["after_revision"] == captured_after[0]
    assert finalized["diff_available"] is True
    assert "task result" in diff
    assert "external edit after crash" not in diff


def test_existing_snapshot_ref_does_not_spawn_for_each_ref(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "tracked.txt").write_text("before", encoding="utf-8")
    service = _service(workspace, tmp_path / "protection")
    service.begin_task("task-ref-read", "session-one")
    token = _task_token("task-ref-read")
    ref = f"refs/stellarcode/tasks/{token}/after"
    revision = service._capture_commit("after", ref)
    original_run_git = service._run_git

    def guarded_run_git(*args, **kwargs):
        assert args[0] != "for-each-ref"
        return original_run_git(*args, **kwargs)

    monkeypatch.setattr(service, "_run_git", guarded_run_git)

    assert service._existing_snapshot_ref(ref) == revision


def test_side_git_environment_removes_inherited_repository_controls(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    service = _service(workspace, tmp_path / "protection")
    monkeypatch.setenv("GIT_OBJECT_DIRECTORY", "poisoned-object-directory")
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "core.fsmonitor")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "malicious-helper")
    monkeypatch.setenv("GIT_PAGER", "less")

    environment = service._git_environment()

    assert environment["GIT_DIR"] == str(service.repository_dir)
    assert environment["GIT_INDEX_FILE"] == str(service.index_file)
    assert environment["GIT_PAGER"] == "cat"
    assert environment["GIT_TERMINAL_PROMPT"] == "0"
    assert "GIT_OBJECT_DIRECTORY" not in environment
    assert "GIT_CONFIG_COUNT" not in environment
    assert "GIT_CONFIG_KEY_0" not in environment
    assert "GIT_CONFIG_VALUE_0" not in environment


def test_side_git_timeout_terminates_process_promptly(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    service = _service(workspace, tmp_path / "protection")
    service.git = Path(sys.executable)
    started = time.monotonic()

    with pytest.raises(WorkspaceProtectionError, match="timed out"):
        service._run_process(
            ("-c", "import time; time.sleep(30)"),
            environment=os.environ.copy(),
            timeout_seconds=0.1,
        )

    assert time.monotonic() - started < 8


def test_rollback_conflict_is_all_or_nothing(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    first = workspace / "first.txt"
    second = workspace / "second.txt"
    first.write_text("first baseline\n", encoding="utf-8")
    second.write_text("second baseline\n", encoding="utf-8")
    service = _service(workspace, tmp_path / "protection")

    service.begin_task("task-conflict", "session-one")
    first.write_text("first from task\n", encoding="utf-8")
    second.write_text("second from task\n", encoding="utf-8")
    service.finalize_task("task-conflict", "completed")
    first.write_text("first manually edited later\n", encoding="utf-8")

    with pytest.raises(WorkspaceRollbackConflict) as raised:
        service.rollback_task("task-conflict", "session-one")

    assert raised.value.paths == ["first.txt"]
    assert first.read_text(encoding="utf-8") == "first manually edited later\n"
    # A conflict must be detected before any other task path is restored.
    assert second.read_text(encoding="utf-8") == "second from task\n"
    status = service.task_status("task-conflict")
    assert status["rolled_back"] is False
    assert status["rollback_available"] is True


def test_rollback_preserves_unrelated_changes_made_after_task(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / "target.txt"
    target.write_text("baseline\n", encoding="utf-8")
    service = _service(workspace, tmp_path / "protection")

    service.begin_task("task-unrelated", "session-one")
    target.write_text("from task\n", encoding="utf-8")
    service.finalize_task("task-unrelated", "completed")

    unrelated = workspace / "manual-after-task.txt"
    unrelated.write_text("keep this manual file\n", encoding="utf-8")
    service.rollback_task("task-unrelated", "session-one")

    assert target.read_text(encoding="utf-8") == "baseline\n"
    assert unrelated.read_text(encoding="utf-8") == "keep this manual file\n"


def test_side_git_snapshots_do_not_touch_user_git_head_index_or_status(tmp_path: Path) -> None:
    workspace = tmp_path / "user-repository"
    workspace.mkdir()
    _git(workspace, "init")
    _git(workspace, "config", "user.name", "Workspace Owner")
    _git(workspace, "config", "user.email", "owner@example.test")
    tracked = workspace / "tracked.txt"
    tracked.write_text("committed\n", encoding="utf-8")
    _git(workspace, "add", "tracked.txt")
    _git(workspace, "commit", "-m", "baseline")

    # Preserve a realistic dirty user index/worktree while snapshots run.
    staged = workspace / "staged.txt"
    staged.write_text("staged by user\n", encoding="utf-8")
    _git(workspace, "add", "staged.txt")
    tracked.write_text("unstaged by user\n", encoding="utf-8")
    (workspace / "untracked.txt").write_text("untracked by user\n", encoding="utf-8")

    head_before = _git(workspace, "rev-parse", "HEAD")
    status_before = _git(workspace, "status", "--porcelain=v1", "-z", "--untracked-files=all")
    index_before = (workspace / ".git" / "index").read_bytes()

    service = _service(workspace, tmp_path / "protection")
    service.begin_task("task-git-isolation", "session-one")
    completed = service.finalize_task("task-git-isolation", "completed")

    assert completed["has_changes"] is False
    assert _git(workspace, "rev-parse", "HEAD") == head_before
    assert (workspace / ".git" / "index").read_bytes() == index_before
    assert (
        _git(workspace, "status", "--porcelain=v1", "-z", "--untracked-files=all")
        == status_before
    )


def test_snapshot_scope_forces_source_files_but_excludes_secrets_and_generated_data(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / ".gitignore").write_text("ignored-source.txt\n", encoding="utf-8")
    source = workspace / "ignored-source.txt"
    secret = workspace / ".env"
    dependency = workspace / "node_modules" / "package" / "index.js"
    trace = workspace / ".stellarcode" / "traces" / "task.jsonl"
    source.write_text("source baseline\n", encoding="utf-8")
    secret.write_text("TOKEN=baseline\n", encoding="utf-8")
    dependency.parent.mkdir(parents=True)
    dependency.write_text("dependency baseline\n", encoding="utf-8")
    trace.parent.mkdir(parents=True)
    trace.write_text("trace baseline\n", encoding="utf-8")
    service = _service(workspace, tmp_path / "protection")

    service.begin_task("task-scope", "session-one")
    source.write_text("source changed\n", encoding="utf-8")
    secret.write_text("TOKEN=changed\n", encoding="utf-8")
    dependency.write_text("dependency changed\n", encoding="utf-8")
    trace.write_text("trace changed\n", encoding="utf-8")
    changes = service.finalize_task("task-scope", "completed")

    assert [item["path"] for item in changes["changed_files"]] == ["ignored-source.txt"]
    service.rollback_task("task-scope", "session-one")
    assert source.read_text(encoding="utf-8") == "source baseline\n"
    assert secret.read_text(encoding="utf-8") == "TOKEN=changed\n"
    assert dependency.read_text(encoding="utf-8") == "dependency changed\n"
    assert trace.read_text(encoding="utf-8") == "trace changed\n"


def test_interrupted_rollback_restores_safety_snapshot_and_can_retry(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    first = workspace / "first.txt"
    second = workspace / "second.txt"
    first.write_text("first baseline\n", encoding="utf-8")
    second.write_text("second baseline\n", encoding="utf-8")
    storage = tmp_path / "protection"
    service = _service(workspace, storage)

    service.begin_task("task-crash-rollback", "session-one")
    first.write_text("first task\n", encoding="utf-8")
    second.write_text("second task\n", encoding="utf-8")
    service.finalize_task("task-crash-rollback", "completed")

    record = service._load_record("task-crash-rollback")
    assert record is not None
    safety = service._capture_commit(
        "simulated pre-rollback safety",
        "refs/stellarcode/tests/rollback-safety",
    )
    record.update(
        rollback_state="in_progress",
        rollback_safety_revision=safety,
    )
    service._write_record(record)
    service._restore_paths(str(record["before_revision"]), ["first.txt"])
    assert first.read_text(encoding="utf-8") == "first baseline\n"
    assert second.read_text(encoding="utf-8") == "second task\n"

    service.close()
    recovered = _service(workspace, storage)
    assert first.read_text(encoding="utf-8") == "first task\n"
    assert second.read_text(encoding="utf-8") == "second task\n"
    status = recovered.task_status("task-crash-rollback")
    assert status["rollback_state"] == "failed"
    assert status["rollback_available"] is True
    notices = recovered.pending_rollback_recoveries()
    assert [item["task_id"] for item in notices] == ["task-crash-rollback"]
    recovered.acknowledge_rollback_recovery("task-crash-rollback")
    assert recovered.pending_rollback_recoveries() == []

    recovered.rollback_task("task-crash-rollback", "session-one")
    assert first.read_text(encoding="utf-8") == "first baseline\n"
    assert second.read_text(encoding="utf-8") == "second baseline\n"


def test_interrupted_rollback_does_not_overwrite_post_crash_manual_edit(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / "target.txt"
    target.write_text("baseline\n", encoding="utf-8")
    storage = tmp_path / "protection"
    service = _service(workspace, storage)

    service.begin_task("task-crash-manual", "session-one")
    target.write_text("from task\n", encoding="utf-8")
    service.finalize_task("task-crash-manual", "completed")
    record = service._load_record("task-crash-manual")
    assert record is not None
    safety = service._capture_commit(
        "simulated safety",
        "refs/stellarcode/tests/manual-safety",
    )
    record.update(rollback_state="in_progress", rollback_safety_revision=safety)
    service._write_record(record)
    target.write_text("manual edit after Runtime crash\n", encoding="utf-8")

    service.close()
    recovered = _service(workspace, storage)

    assert target.read_text(encoding="utf-8") == "manual edit after Runtime crash\n"
    status = recovered.task_status("task-crash-manual")
    assert status["rollback_state"] == "recovery_failed"
    assert status["rollback_available"] is False


def test_failed_rollback_compensation_blocks_retry_and_keeps_a_recovery_notice(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / "target.txt"
    target.write_text("baseline\n", encoding="utf-8")
    service = _service(workspace, tmp_path / "protection")

    service.begin_task("task-compensation-fails", "session-one")
    target.write_text("task result\n", encoding="utf-8")
    service.finalize_task("task-compensation-fails", "completed")

    attempts = 0

    def fail_restore(
        _revision: str,
        _paths: list[str],
        **_kwargs: object,
    ) -> None:
        nonlocal attempts
        attempts += 1
        raise OSError(f"simulated restore failure {attempts}")

    monkeypatch.setattr(service, "_restore_paths", fail_restore)

    with pytest.raises(
        WorkspaceProtectionError,
        match="restoring the pre-rollback safety snapshot also failed",
    ):
        service.rollback_task("task-compensation-fails", "session-one")

    status = service.task_status("task-compensation-fails")
    assert attempts == 2
    assert status["rollback_state"] == "recovery_failed"
    assert status["rollback_available"] is False
    assert [item["task_id"] for item in service.pending_rollback_recoveries()] == [
        "task-compensation-fails"
    ]


def test_recovery_baseline_validation_requires_matching_record_and_git_object(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "target.txt").write_text("baseline\n", encoding="utf-8")
    service = _service(workspace, tmp_path / "protection")
    snapshot = service.begin_task("task-recover", "session-one")

    validated = service.validate_recovery_baseline(
        "task-recover",
        "session-one",
        snapshot["snapshot_id"],
    )
    assert validated["protected"] is True

    with pytest.raises(WorkspaceProtectionError, match="does not match"):
        service.validate_recovery_baseline("task-recover", "session-one", "stale")

    service.finalize_task("task-recover", "failed")
    with pytest.raises(WorkspaceProtectionError, match="already has a final"):
        service.validate_recovery_baseline(
            "task-recover",
            "session-one",
            snapshot["snapshot_id"],
        )

    record_path = service._record_path("task-recover")
    record_path.unlink()
    with pytest.raises(WorkspaceProtectionError, match="record is missing"):
        service.validate_recovery_baseline(
            "task-recover",
            "session-one",
            snapshot["snapshot_id"],
        )


@pytest.mark.parametrize("direction", ["directory_to_file", "file_to_directory"])
def test_task_rollback_supports_file_directory_type_changes(
    tmp_path: Path,
    direction: str,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    node = workspace / "node"
    if direction == "directory_to_file":
        node.mkdir()
        (node / "child.txt").write_text("baseline child\n", encoding="utf-8")
    else:
        node.write_text("baseline file\n", encoding="utf-8")
    service = _service(workspace, tmp_path / "protection")

    service.begin_task(f"task-{direction}", "session-one")
    if direction == "directory_to_file":
        shutil.rmtree(node)
        node.write_text("task file\n", encoding="utf-8")
    else:
        node.unlink()
        node.mkdir()
        (node / "child.txt").write_text("task child\n", encoding="utf-8")
    service.finalize_task(f"task-{direction}", "completed")
    service.rollback_task(f"task-{direction}", "session-one")

    if direction == "directory_to_file":
        assert node.is_dir()
        assert (node / "child.txt").read_text(encoding="utf-8") == "baseline child\n"
    else:
        assert node.is_file()
        assert node.read_text(encoding="utf-8") == "baseline file\n"


def test_type_change_rollback_preserves_unprotected_sensitive_children(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    node = workspace / "node"
    node.write_text("baseline file\n", encoding="utf-8")
    service = _service(workspace, tmp_path / "protection")

    service.begin_task("task-sensitive-child", "session-one")
    node.unlink()
    node.mkdir()
    (node / "task.txt").write_text("task content\n", encoding="utf-8")
    service.finalize_task("task-sensitive-child", "completed")
    secret = node / ".env"
    secret.write_text("TOKEN=user-added-after-task\n", encoding="utf-8")

    with pytest.raises(WorkspaceRollbackConflict) as error:
        service.rollback_task("task-sensitive-child", "session-one")

    assert "node/.env" in error.value.paths
    assert secret.read_text(encoding="utf-8") == "TOKEN=user-added-after-task\n"
    assert (node / "task.txt").read_text(encoding="utf-8") == "task content\n"


@pytest.mark.skipif(os.name != "nt", reason="Windows junction behavior")
def test_snapshot_does_not_follow_a_windows_directory_junction(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    external = tmp_path / "external"
    external.mkdir()
    (external / "private.txt").write_text("outside secret\n", encoding="utf-8")
    junction = workspace / "linked"
    completed = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(junction), str(external)],
        cwd=workspace,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if completed.returncode != 0:
        pytest.skip("This Windows environment cannot create a directory junction")
    try:
        service = _service(workspace, tmp_path / "protection")
        snapshot = service.begin_task("task-junction", "session-one")
        record = service._load_record("task-junction")
        assert record is not None
        tree = service._tree_entries(str(record["before_revision"]))
        assert snapshot["protected"] is True
        assert not any(path == "linked" or path.startswith("linked/") for path in tree)
    finally:
        # rmdir removes the junction itself; it never traverses or deletes target data.
        subprocess.run(
            ["cmd", "/c", "rmdir", str(junction)],
            cwd=workspace,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )


def test_snapshot_is_byte_exact_with_git_clean_filter_and_nested_repository(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _git(workspace, "init")
    _git(workspace, "config", "user.name", "Workspace Owner")
    _git(workspace, "config", "user.email", "owner@example.test")
    _git(workspace, "config", "filter.rewrite.clean", "powershell -NoProfile -Command \"Write-Output FILTERED\"")
    (workspace / ".gitattributes").write_text("*.dat filter=rewrite\n", encoding="utf-8")
    filtered = workspace / "payload.dat"
    filtered.write_bytes(b"exact baseline bytes\r\n")

    nested = workspace / "nested"
    nested.mkdir()
    _git(nested, "init")
    nested_file = nested / "inside.txt"
    nested_file.write_text("nested baseline\n", encoding="utf-8")

    service = _service(workspace, tmp_path / "protection")
    service.begin_task("task-byte-exact", "session-one")
    filtered.write_bytes(b"task bytes\n")
    nested_file.write_text("nested task\n", encoding="utf-8")
    changes = service.finalize_task("task-byte-exact", "completed")
    assert {item["path"] for item in changes["changed_files"]} >= {
        "payload.dat",
        "nested/inside.txt",
    }

    service.rollback_task("task-byte-exact", "session-one")

    assert filtered.read_bytes() == b"exact baseline bytes\r\n"
    assert nested_file.read_text(encoding="utf-8") == "nested baseline\n"


class _EditDuringApproval:
    def __init__(self, edit: Callable[[], None]) -> None:
        self.edit = edit
        self.request: ApprovalRequest | None = None

    def is_enabled(self) -> bool:
        return True

    def set_enabled(self, _enabled: bool) -> None:
        return None

    def clear_approved_all(self) -> None:
        return None

    def request_approval(self, request: ApprovalRequest) -> ApprovalResult:
        self.request = request
        self.edit()
        return ApprovalResult.approved()


@pytest.mark.parametrize("tool_name", ["write_file", "apply_patch", "delete_file"])
def test_approval_to_execution_guard_rejects_changed_target(
    tmp_path: Path,
    tool_name: str,
) -> None:
    target = tmp_path / "guarded.txt"
    target.write_text("state shown in approval\n", encoding="utf-8")
    handler = _EditDuringApproval(
        lambda: target.write_text("manual edit after approval preview\n", encoding="utf-8")
    )
    registry = build_default_registry(tmp_path, hitl_handler=handler)
    if tool_name == "write_file":
        arguments = {"path": "guarded.txt", "content": "agent replacement\n"}
    elif tool_name == "apply_patch":
        arguments = {
            "path": "guarded.txt",
            "edits": [
                {
                    "old_text": "state shown in approval",
                    "new_text": "agent replacement",
                }
            ],
        }
    else:
        arguments = {"path": "guarded.txt"}

    with pytest.raises(ToolExecutionError, match="target changed after its diff was approved"):
        registry.execute(tool_name, arguments)

    assert target.read_text(encoding="utf-8") == "manual edit after approval preview\n"
    assert handler.request is not None
    assert handler.request.change_preview is not None
    assert handler.request.change_preview["before_sha256"]


def test_late_rollback_conflict_preserves_the_new_editor_content(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / "target.txt"
    target.write_text("baseline\n", encoding="utf-8")
    service = _service(workspace, tmp_path / "protection")
    service.begin_task("task-late-conflict", "session-one")
    target.write_text("task result\n", encoding="utf-8")
    service.finalize_task("task-late-conflict", "completed")

    original_capture = service._capture_commit

    def edit_before_compare_and_swap(message: str, ref: str) -> str:
        if "rollback compare-and-swap" in message:
            target.write_text("late editor content\n", encoding="utf-8")
        return original_capture(message, ref)

    monkeypatch.setattr(service, "_capture_commit", edit_before_compare_and_swap)

    with pytest.raises(WorkspaceRollbackConflict):
        service.rollback_task("task-late-conflict", "session-one")

    assert target.read_text(encoding="utf-8") == "late editor content\n"
    status = service.task_status("task-late-conflict")
    assert status["rollback_state"] == "failed"
    assert status["rollback_available"] is True


def test_editor_change_during_result_snapshot_is_not_overwritten_by_compensation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / "target.txt"
    target.write_text("baseline\n", encoding="utf-8")
    service = _service(workspace, tmp_path / "protection")
    service.begin_task("task-result-race", "session-one")
    target.write_text("task result\n", encoding="utf-8")
    service.finalize_task("task-result-race", "completed")

    original_capture = service._capture_commit

    def fail_result_snapshot(message: str, ref: str) -> str:
        if "rollback result" in message:
            target.write_text("manual edit during result snapshot\n", encoding="utf-8")
            raise OSError("simulated result snapshot failure")
        return original_capture(message, ref)

    monkeypatch.setattr(service, "_capture_commit", fail_result_snapshot)

    with pytest.raises(WorkspaceProtectionError, match="also failed"):
        service.rollback_task("task-result-race", "session-one")

    assert target.read_text(encoding="utf-8") == "manual edit during result snapshot\n"
    status = service.task_status("task-result-race")
    assert status["rollback_state"] == "recovery_failed"
    assert status["rollback_available"] is False


def test_project_side_git_storage_has_one_process_owner(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    storage = tmp_path / "protection"
    first = _service(workspace, storage)

    second = WorkspaceProtectionService(workspace, storage)
    assert second.available is False
    rejected = second.begin_task("task-locked", "session-one")
    assert rejected["protected"] is False
    assert "already owned" in rejected["error"]
    with pytest.raises(WorkspaceProtectionError, match="already owned"):
        second.finalize_task("task-locked", "completed")
    with pytest.raises(WorkspaceProtectionError, match="already owned"):
        second.rollback_task("task-locked", "session-one")
    with pytest.raises(WorkspaceProtectionError, match="already owned"):
        second.task_diff("task-locked", "session-one")

    first.close()
    reopened = _service(workspace, storage)
    assert reopened.begin_task("task-reopened", "session-one")["protected"] is True


@pytest.mark.skipif(os.name != "nt", reason="NTFS alternate data streams")
@pytest.mark.parametrize("target_kind", ["file", "directory"])
def test_rollback_refuses_to_delete_alternate_data_streams(
    tmp_path: Path,
    target_kind: str,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    node = workspace / "node"
    node.write_text("baseline file\n", encoding="utf-8")
    service = _service(workspace, tmp_path / "protection")
    service.begin_task(f"task-ads-{target_kind}", "session-one")
    if target_kind == "directory":
        node.unlink()
        node.mkdir()
        (node / "task.txt").write_text("task result\n", encoding="utf-8")
    else:
        node.write_text("task result\n", encoding="utf-8")
    service.finalize_task(f"task-ads-{target_kind}", "completed")

    stream = Path(f"{node}:manual")
    try:
        stream.write_text("manual ADS\n", encoding="utf-8")
    except OSError:
        pytest.skip("The temporary filesystem does not support NTFS alternate streams")

    with pytest.raises(WorkspaceRollbackConflict):
        service.rollback_task(f"task-ads-{target_kind}", "session-one")

    assert stream.read_text(encoding="utf-8") == "manual ADS\n"
    if target_kind == "directory":
        assert (node / "task.txt").read_text(encoding="utf-8") == "task result\n"
    else:
        assert node.read_text(encoding="utf-8") == "task result\n"


def test_rollback_checks_each_path_again_before_mutating_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    first = workspace / "a.txt"
    second = workspace / "b.txt"
    service = _service(workspace, tmp_path / "protection")
    service.begin_task("task-step-cas", "session-one")
    first.write_text("created by task\n", encoding="utf-8")
    second.write_text("created by task\n", encoding="utf-8")
    service.finalize_task("task-step-cas", "completed")

    original_delete = service._delete_restored_path

    def edit_second_after_first(path: Path) -> None:
        original_delete(path)
        if path.name == "a.txt":
            second.write_text("late external editor\n", encoding="utf-8")

    monkeypatch.setattr(service, "_delete_restored_path", edit_second_after_first)

    with pytest.raises(WorkspaceProtectionError):
        service.rollback_task("task-step-cas", "session-one")

    assert second.read_text(encoding="utf-8") == "late external editor\n"
    assert service.task_status("task-step-cas")["rollback_state"] == "recovery_failed"
