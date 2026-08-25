"""JSONL Sidecar server bridging the desktop shell and Python Runtime.

Requests are accepted on stdin, responses/events are written to stdout, and all task
execution stays in worker threads so the protocol reader remains responsive to cancel
and approval requests.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from stellarcode import __version__
from stellarcode.cancellation import TaskCancelledError
from stellarcode.mcp import McpServerConfig
from stellarcode.rag.index import is_indexable_file
from stellarcode.protection import WorkspaceProtectionError, WorkspaceRollbackConflict
from stellarcode.runtime.attachments import PreparedAttachments
from stellarcode.runtime.core import RuntimeSession, RuntimeSettings
from stellarcode.runtime.protocol import (
    PROTOCOL_VERSION,
    JsonLineWriter,
    RuntimeEventEmitter,
    response,
)
from stellarcode.runtime.recovery import EventJournal
from stellarcode.runtime.task_state import RuntimeTaskStateMachine, TaskRoutePhase


class SidecarServer:
    def __init__(
        self,
        workspace: Path,
        writer: JsonLineWriter,
        data_dir: Path | None = None,
        worktree_dir: Path | None = None,
        *,
        max_iterations: int = 8,
        max_parallel_tools: int = 4,
        tool_batch_timeout: float = 90,
        plan_workers: int = 4,
        team_workers: int = 2,
        team_retries: int = 2,
        context_window: int = 200_000,
        rag_auto_retrieval: bool = True,
        diagnostics_lsp_enabled: bool = False,
        diagnostics_lsp_command: str = "",
        diagnostics_lsp_args: tuple[str, ...] = (),
        diagnostics_lsp_timeout: float = 20.0,
    ) -> None:
        self.default_workspace = workspace.resolve()
        self.data_dir = data_dir.resolve() if data_dir else None
        self.worktree_dir = worktree_dir.resolve() if worktree_dir else None
        self.writer = writer
        self.events = RuntimeEventEmitter(writer)
        self.runtime: RuntimeSession | None = None
        self.project_id: str | None = None
        self.runtimes: dict[str, RuntimeSession] = {}
        self.project_emitters: dict[str, RuntimeEventEmitter] = {}
        self.session_projects: dict[str, str] = {}
        self._task_lock = threading.RLock()
        self.task_state = RuntimeTaskStateMachine(self._task_lock)
        # Compatibility aliases for integrations that inspect live routing.
        # All mutations go through ``task_state`` transitions below.
        self.task_routes = self.task_state.routes
        self.session_tasks = self.task_state.tasks_by_session
        self.project_active_sessions: dict[str, str] = {}
        self.rag_jobs: dict[str, str] = {}
        self.diagnostics_jobs: dict[str, str] = {}
        self.diagnostics_cancel_events: dict[str, threading.Event] = {}
        self.active_task_id: str | None = None
        self.active_session_id: str | None = None
        self.active_rag_job_id: str | None = None
        self.active_diagnostics_job_id: str | None = None
        self._diagnostics_cancel_event: threading.Event | None = None
        self._rag_lock = threading.RLock()
        self._diagnostics_lock = threading.RLock()
        self.runtime_options = {
            "max_iterations": max_iterations,
            "max_parallel_tools": max_parallel_tools,
            "tool_batch_timeout": tool_batch_timeout,
            "plan_workers": plan_workers,
            "team_workers": team_workers,
            "team_retries": team_retries,
            "context_window": context_window,
            "rag_auto_retrieval": rag_auto_retrieval,
            "diagnostics_lsp_enabled": diagnostics_lsp_enabled,
            "diagnostics_lsp_command": diagnostics_lsp_command,
            "diagnostics_lsp_args": diagnostics_lsp_args,
            "diagnostics_lsp_timeout": diagnostics_lsp_timeout,
        }

    def start(self) -> None:
        self.events.emit(
            "runtime.ready",
            {
                "runtime_version": __version__,
                "capabilities": [
                    "projects",
                    "multi_session",
                    "concurrent_projects",
                    "concurrent_conversations",
                    "concurrent_approvals",
                    "session_persistence",
                    "session_execution_persistence",
                    "react",
                    "plan",
                    "team",
                    "hitl",
                    "access_modes",
                    "tools",
                    "mcp",
                    "task_cancellation",
                    "conversation_trace",
                    "automatic_recovery",
                    "event_replay",
                    "task_checkpoints",
                    "assistant_streaming",
                    "provider_usage",
                    "mcp_management",
                    "rag_management",
                    "rag_multi_source_index",
                    "memory_management",
                    "skill_management",
                    "browser_management",
                    "workspace_diagnostics",
                    "change_previews",
                    "task_git_snapshots",
                    "task_git_worktrees",
                    "task_rollback",
                    "prompt_observability",
                ],
            },
        )

    def handle(self, request: dict[str, Any]) -> bool:
        request_id = str(request.get("request_id") or "")
        if request.get("kind") != "request":
            self._error(request_id, "invalid_message", "kind must be request")
            return True
        if request.get("protocol_version") != PROTOCOL_VERSION:
            self._error(request_id, "unsupported_protocol", "unsupported protocol version")
            return True
        method = request.get("method")
        try:
            params = request.get("params") or {}
            if not isinstance(params, dict):
                raise ValueError("params must be an object")
            trace_runtime = self._runtime_for_request(params, strict=False)
            if trace_runtime is not None:
                trace_runtime.trace_recorder.record(
                    "runtime_request",
                    request_id=request_id,
                    method=method,
                    params=params,
                )
            if method == "runtime.ping":
                self.writer(response(request_id, result={"runtime_version": __version__}))
            elif method == "workspace.open":
                self._open_workspace(request_id, params)
            elif method == "workspace.close":
                self._close_workspace(request_id, params)
            elif method == "runtime.set_access_mode":
                self._set_access_mode(request_id, params)
            elif method == "session.list":
                runtime = self._require_runtime(params)
                self.writer(response(request_id, result={"conversations": runtime.list_conversations()}))
            elif method == "session.create":
                self._create_session(request_id, params)
            elif method == "session.open":
                self._open_session(request_id, params)
            elif method == "session.rename":
                self._rename_session(request_id, params)
            elif method == "session.delete":
                self._delete_session(request_id, params)
            elif method == "session.reset":
                self._reset_session(request_id, params)
            elif method == "session.set_mode":
                self._set_mode(request_id, params)
            elif method == "session.set_trace":
                self._set_trace(request_id, params)
            elif method == "prompt.snapshot":
                self._prompt_snapshot(request_id, params)
            elif method == "event.replay":
                self._replay_events(request_id, params)
            elif method == "mcp.list":
                runtime = self._require_runtime(params)
                self.writer(response(request_id, result=runtime.mcp_snapshot()))
            elif method == "mcp.install":
                self._install_mcp(request_id, params)
            elif method == "mcp.set_enabled":
                self._set_mcp_enabled(request_id, params)
            elif method == "mcp.restart":
                self._restart_mcp(request_id, params)
            elif method == "mcp.remove":
                self._remove_mcp(request_id, params)
            elif method == "mcp.logs":
                self._mcp_logs(request_id, params)
            elif method == "rag.snapshot":
                self._rag_snapshot(request_id, params)
            elif method == "rag.add_sources":
                self._add_rag_sources(request_id, params)
            elif method == "rag.remove_source":
                self._remove_rag_source(request_id, params)
            elif method == "rag.index":
                self._start_rag_index(request_id, params)
            elif method == "rag.clear":
                self._clear_rag_index(request_id, params)
            elif method == "memory.list":
                self._list_memory(request_id, params)
            elif method == "memory.save":
                self._save_memory(request_id, params)
            elif method == "memory.delete":
                self._delete_memory(request_id, params)
            elif method == "memory.clear":
                self._clear_memory(request_id, params)
            elif method == "skill.list":
                self._list_skills(request_id, params)
            elif method == "skill.get":
                self._get_skill(request_id, params)
            elif method == "skill.diff":
                self._skill_diff(request_id, params)
            elif method == "skill.set_enabled":
                self._set_skill_enabled(request_id, params)
            elif method == "skill.reload":
                self._reload_skills(request_id, params)
            elif method == "skill.update":
                self._change_bundled_skill(request_id, params, "update")
            elif method == "skill.keep_custom":
                self._change_bundled_skill(request_id, params, "keep_custom")
            elif method == "skill.restore_default":
                self._change_bundled_skill(request_id, params, "restore_default")
            elif method == "browser.snapshot":
                self._browser_snapshot(request_id, params)
            elif method == "browser.probe":
                self._probe_browser(request_id, params)
            elif method == "browser.connect":
                self._connect_browser(request_id, params)
            elif method == "browser.disconnect":
                self._disconnect_browser(request_id, params)
            elif method == "browser.tabs":
                self._browser_tabs(request_id, params)
            elif method == "diagnostics.snapshot":
                self._diagnostics_snapshot(request_id, params)
            elif method == "diagnostics.run":
                self._start_diagnostics(request_id, params)
            elif method == "diagnostics.cancel":
                self._cancel_diagnostics(request_id, params)
            elif method == "task.submit":
                self._submit_task(request_id, params)
            elif method == "task.recover":
                self._recover_task(request_id, params)
            elif method == "task.cancel":
                self._cancel_task(request_id, params)
            elif method == "task.rollback":
                self._rollback_task(request_id, params)
            elif method == "task.diff":
                self._task_diff(request_id, params)
            elif method == "approval.resolve":
                self._resolve_approval(request_id, params)
            elif method == "runtime.shutdown":
                self.writer(response(request_id, result={}))
                self._emit_event("runtime.shutdown", {"reason": "requested"})
                return False
            else:
                self._error(request_id, "unknown_method", f"unknown method: {method}")
        except KeyError as exc:
            self._error(request_id, "session_not_found", str(exc))
        except ValueError as exc:
            self._error(request_id, "invalid_message", str(exc))
        except RuntimeError as exc:
            self._error(request_id, "runtime_unavailable", str(exc))
        except Exception as exc:
            self._error(request_id, "internal_error", f"{type(exc).__name__}: {exc}")
        return True

    def close(self) -> None:
        with self._diagnostics_lock:
            for cancel_event in self.diagnostics_cancel_events.values():
                cancel_event.set()
        for runtime in list(self.runtimes.values()):
            runtime.close()
        self.runtimes.clear()
        self.project_emitters.clear()
        self.runtime = None

    def _open_workspace(self, request_id: str, params: dict[str, Any]) -> None:
        workspace = Path(params.get("workspace") or self.default_workspace).resolve()
        project_id = str(params.get("project_id") or _fallback_project_id(workspace))
        root_data_dir = self.data_dir or (Path.home() / ".stellarcode" / "desktop-runtime")
        runtime = self.runtimes.get(project_id)
        pending_finalizers: list[tuple[RuntimeSession, str, str]] = []
        if runtime is not None:
            if runtime.workspace.resolve() != workspace:
                raise RuntimeError("project id is already loaded for another workspace")
            emitter = self.project_emitters[project_id]
        else:
            event_journal = EventJournal(
                root_data_dir.resolve() / "projects" / project_id / "events.jsonl"
            )
            emitter = RuntimeEventEmitter(self.writer, event_journal)
            runtime_holder: dict[str, RuntimeSession] = {}

            def emit_runtime_event(
                event_type: str,
                data: dict[str, Any],
                session_id: str,
                task_id: str | None = None,
            ) -> dict[str, Any]:
                target = runtime_holder.get("runtime")
                if target is not None:
                    target.record_runtime_event(event_type, data, session_id, task_id)
                return emitter.emit(
                    event_type,
                    data,
                    session_id=session_id,
                    task_id=task_id,
                )

            runtime = RuntimeSession(
                RuntimeSettings(
                    workspace=workspace,
                    project_id=project_id,
                    data_dir=self.data_dir,
                    worktree_dir=self.worktree_dir,
                    **self.runtime_options,
                ),
                emit_runtime_event,
                event_journal=event_journal,
            )
            runtime_holder["runtime"] = runtime
            self.runtimes[project_id] = runtime
            self.project_emitters[project_id] = emitter
            for session_id in runtime.conversations:
                self.session_projects[session_id] = project_id

            for recovery in runtime.pending_recoveries():
                task_id = str(recovery["task_id"])
                session_id = str(recovery["session_id"])
                if runtime.event_journal.task_is_terminal(task_id):
                    runtime.complete_task_checkpoint(task_id)
                    continue
                if recovery.get("status") == "finalize_pending":
                    self._register_task_route(
                        runtime,
                        session_id,
                        task_id,
                        phase="finalizing",
                    )
                    pending_finalizers.append((runtime, session_id, task_id))

        self.project_id = project_id
        self.runtime = runtime
        self.events = emitter
        self.active_session_id = self.project_active_sessions.get(project_id) or None
        self.active_task_id = (
            self.session_tasks.get(self.active_session_id or "")
            if self.active_session_id
            else None
        )
        self.active_rag_job_id = self.rag_jobs.get(project_id)
        self.active_diagnostics_job_id = self.diagnostics_jobs.get(project_id)
        self._diagnostics_cancel_event = self.diagnostics_cancel_events.get(project_id)
        unfinished_recoveries = [
            recovery
            for recovery in runtime.pending_recoveries()
            if not runtime.event_journal.task_is_terminal(str(recovery["task_id"]))
        ]
        active_route_ids = self.task_state.active_task_ids()
        recoveries = _desktop_visible_recoveries(unfinished_recoveries, active_route_ids)
        recovery = recoveries[0] if recoveries else None
        active_tasks = [
            {
                "task_id": task_id,
                "session_id": route.session_id,
                "phase": route.phase,
            }
            for task_id, route in self.task_state.routes_for_project(project_id)
            if route.runtime is runtime
        ]
        result = {
            "project_id": project_id,
            "workspace": str(workspace),
            "provider": runtime.provider_name,
            "model": runtime.model,
            "conversation_count": len(runtime.conversations),
            "access_mode": runtime.access_mode,
            "recovery": recovery,
            "recoveries": recoveries,
            "active_tasks": active_tasks,
        }
        self.writer(response(request_id, result=result))
        emitter.emit(
            "workspace.opened",
            result,
            session_id=f"workspace-{project_id}",
        )
        # Send workspace.open response/state before a very fast finalizer can
        # publish the recovered terminal event. This keeps the desktop state
        # transition ordered even when no retry delay is required.
        for pending_finalizer in pending_finalizers:
            threading.Thread(
                target=self._retry_pending_finalization,
                args=pending_finalizer,
                name="stellarcode-workspace-finalizer",
                daemon=True,
            ).start()
        for rollback_recovery in runtime.pending_rollback_recoveries():
            task_id = str(rollback_recovery.get("task_id") or "")
            session_id = str(rollback_recovery.get("session_id") or "")
            if not task_id or not session_id:
                continue
            recovery_failed = rollback_recovery.get("rollback_state") == "recovery_failed"
            self._emit_event(
                "task.rollback.failed",
                {
                    "snapshot_id": str(rollback_recovery.get("snapshot_id") or ""),
                    "code": (
                        "rollback_recovery_failed"
                        if recovery_failed
                        else "rollback_interrupted_recovered"
                    ),
                    "message": str(rollback_recovery.get("error") or ""),
                    "conflicted_paths": [],
                },
                session_id=session_id,
                task_id=task_id,
            )
            runtime.acknowledge_rollback_recovery(task_id)

    def _close_workspace(self, request_id: str, params: dict[str, Any]) -> None:
        runtime = self._require_runtime(params)
        project_id = self._runtime_project_id(runtime)
        if self._project_has_active_tasks(project_id) or self.rag_jobs.get(project_id) or self.diagnostics_jobs.get(project_id):
            raise RuntimeError(
                "cannot close workspace while a task, RAG index, or diagnostics run is active"
            )
        emitter = self.project_emitters.get(project_id) or self.events
        was_current = self.runtime is runtime
        runtime.close()
        self.runtimes.pop(project_id, None)
        self.project_emitters.pop(project_id, None)
        self.project_active_sessions.pop(project_id, None)
        for session_id, owner in list(self.session_projects.items()):
            if owner == project_id:
                self.session_projects.pop(session_id, None)
        if was_current:
            self.runtime = None
            self.events = RuntimeEventEmitter(self.writer)
            self.project_id = None
            self.active_session_id = None
            self.active_task_id = None
        self.writer(response(request_id, result={}))
        if project_id:
            emitter.emit(
                "workspace.closed",
                {"project_id": project_id},
                session_id=f"workspace-{project_id}",
            )

    def _create_session(self, request_id: str, params: dict[str, Any]) -> None:
        runtime = self._require_runtime(params)
        snapshot = runtime.create_conversation(
            title=str(params.get("title") or "New conversation"),
            mode=str(params.get("mode") or "react"),
        )
        session_id = str(snapshot["id"])
        self.session_projects[session_id] = runtime.project_id
        self._set_project_active_context(runtime, session_id, None)
        result = {
            **snapshot,
            "session_id": session_id,
            "provider": runtime.provider_name,
            "model": runtime.model,
        }
        self.writer(response(request_id, result=result))
        self._emit_event(
            "session.created",
            {
                "workspace": str(runtime.workspace),
                "mode": snapshot["mode"],
                "title": snapshot["title"],
            },
            session_id=session_id,
        )
        self._emit_event(
            "session.snapshot",
            snapshot,
            session_id=session_id,
        )

    def _set_access_mode(self, request_id: str, params: dict[str, Any]) -> None:
        runtime = self._require_runtime(params)
        session_id = self._session_id_for_runtime(params, runtime)
        if not session_id:
            raise ValueError("session_id must not be empty")
        self._assert_session_idle(session_id)
        mode = str(params.get("mode") or "")
        result = runtime.set_conversation_access_mode(session_id, mode)
        self.writer(response(request_id, result=result))
        self._emit_event(
            "access.mode_changed",
            {"mode": mode},
            session_id=session_id,
        )

    def _open_session(self, request_id: str, params: dict[str, Any]) -> None:
        runtime = self._require_runtime(params)
        session_id = str(params.get("session_id") or "")
        snapshot = runtime.open_conversation(session_id)
        self.session_projects[session_id] = runtime.project_id
        self._set_project_active_context(runtime, session_id, self.session_tasks.get(session_id))
        self.writer(response(request_id, result=snapshot))
        self._emit_event("session.opened", {"title": snapshot["title"]}, session_id=session_id)
        self._emit_event("session.snapshot", snapshot, session_id=session_id)

    def _rename_session(self, request_id: str, params: dict[str, Any]) -> None:
        runtime = self._require_runtime(params)
        session_id = str(params.get("session_id") or "")
        self._assert_session_idle(session_id)
        metadata = runtime.rename_conversation(session_id, str(params.get("title") or ""))
        self.writer(response(request_id, result=metadata))
        self._emit_event(
            "session.renamed",
            {"title": metadata["title"]},
            session_id=session_id,
        )

    def _delete_session(self, request_id: str, params: dict[str, Any]) -> None:
        runtime = self._require_runtime(params)
        session_id = str(params.get("session_id") or "")
        self._assert_session_idle(session_id)
        metadata = runtime.delete_conversation(session_id)
        if self.runtime is runtime and self.active_session_id == session_id:
            self.active_session_id = None
            self.active_task_id = None
        self.writer(response(request_id, result=metadata))
        self._emit_event(
            "session.deleted",
            {"title": metadata["title"]},
            session_id=session_id,
        )
        self.session_projects.pop(session_id, None)

    def _reset_session(self, request_id: str, params: dict[str, Any]) -> None:
        runtime = self._require_runtime(params)
        session_id = self._session_id_for_runtime(params, runtime)
        self._assert_session_idle(session_id)
        cleared = runtime.reset(session_id)
        self._emit_event(
            "session.reset",
            {"cleared_message_count": cleared},
            session_id=session_id,
        )
        self.writer(response(request_id, result={"cleared_message_count": cleared}))

    def _set_mode(self, request_id: str, params: dict[str, Any]) -> None:
        runtime = self._require_runtime(params)
        session_id = self._session_id_for_runtime(params, runtime)
        self._assert_session_idle(session_id)
        mode = str(params.get("mode") or "")
        runtime.set_mode(session_id, mode)
        self.writer(response(request_id, result={"mode": mode}))

    def _set_trace(self, request_id: str, params: dict[str, Any]) -> None:
        runtime = self._require_runtime(params)
        session_id = self._session_id_for_runtime(params, runtime)
        self._assert_session_idle(session_id)
        enabled = params.get("enabled")
        if not isinstance(enabled, bool):
            raise ValueError("enabled must be a boolean")
        result = runtime.set_trace(session_id, enabled)
        self.writer(response(request_id, result=result))
        self._emit_event(
            "trace.status_changed",
            result,
            session_id=session_id,
        )

    def _prompt_snapshot(self, request_id: str, params: dict[str, Any]) -> None:
        runtime = self._require_runtime(params)
        session_id = _bounded_string(
            self._session_id_for_runtime(params, runtime),
            "session_id",
            128,
        )
        include_memory = params.get("include_memory", False)
        if not isinstance(include_memory, bool):
            raise ValueError("include_memory must be a boolean")
        self.writer(
            response(
                request_id,
                result=runtime.prompt_snapshot(
                    session_id,
                    include_sensitive=include_memory,
                ),
            )
        )

    def _submit_task(self, request_id: str, params: dict[str, Any]) -> None:
        runtime = self._require_runtime(params)
        prompt = str(params.get("prompt") or "").strip()
        attachments = params.get("attachments")
        session_id = self._session_id_for_runtime(params, runtime)
        if not session_id:
            raise ValueError("session_id must not be empty")
        if not prompt and not attachments:
            raise ValueError("prompt or attachments must not be empty")
        runtime.open_conversation(session_id)
        with self._task_lock:
            if session_id in self.session_tasks:
                self._error(request_id, "task_busy", "this conversation already has a running task")
                return
            if self.rag_jobs.get(runtime.project_id):
                self._error(request_id, "task_busy", "the project RAG index is being rebuilt")
                return
            task_id = f"task-{uuid.uuid4().hex}"
            self._register_task_route(runtime, session_id, task_id)
        try:
            prepared = runtime.prepare_task(task_id, session_id, prompt, attachments)
        except Exception:
            self._release_task_route(task_id)
            raise
        self._set_project_active_context(runtime, session_id, task_id)
        self.writer(response(request_id, result={"task_id": task_id}))
        threading.Thread(
            target=self._run_task,
            args=(runtime, session_id, task_id, prompt, prepared, False),
            name=f"stellarcode-runtime-task-{task_id[-8:]}",
            daemon=True,
        ).start()

    def _run_task(
        self,
        runtime: RuntimeSession,
        session_id: str,
        task_id: str,
        prompt: str,
        prepared: PreparedAttachments | None = None,
        recovered: bool = False,
    ) -> None:
        if self.task_state.route(task_id) is None:
            # Preserve the embedded/test entry point while keeping all live
            # execution inside the same state machine as protocol-submitted tasks.
            self._register_task_route(runtime, session_id, task_id)
        route = self.task_state.route(task_id)
        if route is not None and route.phase == "accepted":
            self.task_state.transition(task_id, "running")
        started = time.monotonic()
        pending_recoveries = getattr(runtime, "pending_recoveries", None)
        if callable(pending_recoveries):
            recoveries = pending_recoveries()
        else:
            pending_recovery = getattr(runtime, "pending_recovery", lambda: None)()
            recoveries = [pending_recovery] if pending_recovery else []
        recovery = next(
            (item for item in recoveries if item.get("task_id") == task_id),
            {},
        )
        answer_was_ready = recovered and recovery.get("status") == "answer_ready"
        protection_status = getattr(runtime, "task_protection_status", None)
        protection = (
            protection_status(task_id)
            if callable(protection_status)
            else {
                "protected": False,
                "status": "unavailable",
                "error": "Task protection status is unavailable.",
            }
        )
        # Progress is helpful to a live desktop, but it is not part of task
        # correctness.  A broken stdout/UI transport must not prevent Agent
        # execution or change a successful outcome into a failed task.
        self._emit_task_progress_best_effort(
            "task.started",
            {
                "mode": runtime.get_mode(session_id),
                "prompt_preview": prompt[:240],
                "recovered": recovered,
                "recovery_attempt": int(recovery.get("recovery_attempts") or 0),
                "started_at": recovery.get("started_at"),
                "protection": protection,
            },
            session_id=session_id,
            task_id=task_id,
        )
        self._emit_task_progress_best_effort(
            "assistant.thinking",
            {"status": "started"},
            session_id=session_id,
            task_id=task_id,
        )
        terminal_type = "task.completed"
        terminal_outcome = "completed"
        terminal_data: dict[str, Any]
        try:
            answer = (
                runtime.resume_task(task_id, session_id)
                if recovered
                else runtime.run(
                    prompt,
                    task_id,
                    session_id,
                    prepared=prepared,
                )
            )
            terminal_data = {
                "status": "completed",
                "elapsed_ms": int((time.monotonic() - started) * 1000),
            }
        except TaskCancelledError:
            terminal_type = "task.cancelled"
            terminal_outcome = "cancelled"
            terminal_data = {
                "status": "cancelled",
                "reason": "user",
                "elapsed_ms": int((time.monotonic() - started) * 1000),
            }
        except Exception as exc:
            terminal_type = "task.failed"
            terminal_outcome = "failed"
            terminal_data = {
                "status": "failed",
                "error_code": "task_failed",
                "message": f"{type(exc).__name__}: {exc}",
                "recoverable": True,
                "elapsed_ms": int((time.monotonic() - started) * 1000),
            }
        else:
            # Runtime.run has already persisted the final answer in the
            # conversation snapshot. Delivery of the convenience UI event is
            # therefore best-effort and must never turn a successful task into
            # task.failed.
            if not answer_was_ready:
                self._emit_task_progress_best_effort(
                    "assistant.completed",
                    {"content": answer, "finish_reason": "stop"},
                    session_id=session_id,
                    task_id=task_id,
                )
        self._emit_task_progress_best_effort(
            "assistant.thinking",
            {"status": "finished"},
            session_id=session_id,
            task_id=task_id,
        )
        # Terminal handling is a separate durable state transition: first keep
        # the intended outcome/checkpoint, then finalize workspace changes, and
        # only then commit the journaled terminal event.
        terminal_finished = self._finish_task_terminal(
            runtime,
            session_id,
            task_id,
            terminal_type,
            terminal_outcome,
            terminal_data,
        )
        if terminal_finished:
            self._release_task_route(task_id)

    def _finish_task_terminal(
        self,
        runtime: RuntimeSession,
        session_id: str,
        task_id: str,
        terminal_type: str,
        outcome: str,
        terminal_data: dict[str, Any],
    ) -> bool:
        self.task_state.transition(task_id, "finalizing")
        # Phase 1: terminal intent must be durable before the POST snapshot. A
        # crash from this point onward resumes only finalization, never Agent
        # execution.
        try:
            runtime.mark_task_finalize_pending(task_id, outcome, terminal_data)
        except Exception as exc:
            self._emit_task_progress_best_effort(
                "task.finalization.pending",
                {
                    "outcome": outcome,
                    "message": (
                        "Terminal intent could not be persisted; the task remains blocked "
                        f"for manual recovery: {type(exc).__name__}: {exc}"
                    ),
                    "recoverable": False,
                },
                session_id=session_id,
                task_id=task_id,
            )
            return False
        try:
            finalized_outcome, finalized_data = runtime.finalize_pending_task(task_id, session_id)
            if finalized_outcome != outcome:
                outcome = finalized_outcome
                terminal_type = {
                    "completed": "task.completed",
                    "cancelled": "task.cancelled",
                    "failed": "task.failed",
                }[finalized_outcome]
        except Exception as exc:
            runtime.mark_task_finalize_pending(task_id, outcome, terminal_data, exc)
            self._emit_task_progress_best_effort(
                "task.finalization.pending",
                {
                    "outcome": outcome,
                    "message": f"{type(exc).__name__}: {exc}",
                    "recoverable": True,
                },
                session_id=session_id,
                task_id=task_id,
            )
            threading.Thread(
                target=self._retry_pending_finalization,
                args=(runtime, session_id, task_id),
                name="stellarcode-task-finalizer",
                daemon=True,
            ).start()
            return False
        try:
            return self._commit_terminal_event(
                runtime,
                session_id,
                task_id,
                terminal_type,
                finalized_data,
            )
        except Exception as exc:
            try:
                self._emit_event(
                    "task.finalization.pending",
                    {
                        "outcome": outcome,
                        "message": f"{type(exc).__name__}: {exc}",
                        "recoverable": True,
                    },
                    session_id=session_id,
                    task_id=task_id,
                )
            except Exception:
                pass
            threading.Thread(
                target=self._retry_pending_finalization,
                args=(runtime, session_id, task_id),
                name="stellarcode-task-finalizer",
                daemon=True,
            ).start()
            return False

    def _commit_terminal_event(
        self,
        runtime: RuntimeSession,
        session_id: str,
        task_id: str,
        terminal_type: str,
        terminal_data: dict[str, Any],
    ) -> bool:
        # The event journal is the commit point. If transport delivery or
        # checkpoint cleanup fails after append, restart/retry detects the
        # terminal task and performs cleanup without emitting a second event.
        if not runtime.event_journal.task_is_terminal(task_id):
            try:
                self._emit_event(
                    terminal_type,
                    terminal_data,
                    session_id=session_id,
                    task_id=task_id,
                )
            except Exception:
                if not runtime.event_journal.task_is_terminal(task_id):
                    raise
        if not runtime.event_journal.task_is_terminal(task_id):
            return False
        try:
            runtime.complete_task_checkpoint(task_id)
        except Exception:
            # The journaled terminal remains authoritative. A restart or a later
            # cleanup retry will remove the stale checkpoint idempotently.
            pass
        return True

    def _retry_pending_finalization(
        self,
        runtime: RuntimeSession,
        session_id: str,
        task_id: str,
    ) -> None:
        delay = 0.5
        while self.runtimes.get(runtime.project_id) is runtime:
            time.sleep(delay)
            with self._task_lock:
                route = self.task_routes.get(task_id)
                if route is None or route.runtime is not runtime:
                    return
            try:
                if runtime.event_journal.task_is_terminal(task_id):
                    try:
                        runtime.complete_task_checkpoint(task_id)
                    except Exception:
                        delay = min(15.0, delay * 2)
                        continue
                    self._release_task_route(task_id)
                    return
                outcome, terminal_data = runtime.finalize_pending_task(task_id, session_id)
                terminal_type = {
                    "completed": "task.completed",
                    "cancelled": "task.cancelled",
                    "failed": "task.failed",
                }[outcome]
                terminal_committed = self._commit_terminal_event(
                    runtime,
                    session_id,
                    task_id,
                    terminal_type,
                    terminal_data,
                )
                if not terminal_committed:
                    raise RuntimeError("terminal event was not committed")
            except Exception:
                delay = min(15.0, delay * 2)
                continue
            self._release_task_route(task_id)
            return

    def _recover_task(self, request_id: str, params: dict[str, Any]) -> None:
        runtime = self._require_runtime(params)
        task_id = str(params.get("task_id") or "")
        session_id = str(params.get("session_id") or "")
        if not task_id or not session_id:
            raise ValueError("task_id and session_id must not be empty")
        runtime.open_conversation(session_id)
        with self._task_lock:
            if session_id in self.session_tasks:
                self._error(request_id, "task_busy", "this conversation already has a running task")
                return
            self._register_task_route(runtime, session_id, task_id)
        try:
            checkpoint = runtime.prepare_recovery(task_id, session_id)
        except Exception:
            self._release_task_route(task_id)
            raise
        self._set_project_active_context(runtime, session_id, task_id)
        self.writer(
            response(
                request_id,
                result={
                    "accepted": True,
                    "task_id": task_id,
                    "recovery_attempt": int(checkpoint.get("recovery_attempts") or 0),
                },
            )
        )
        threading.Thread(
            target=self._run_task,
            args=(
                runtime,
                session_id,
                task_id,
                str(checkpoint.get("prompt") or ""),
                None,
                True,
            ),
            name=f"stellarcode-runtime-recovery-{task_id[-8:]}",
            daemon=True,
        ).start()

    def _replay_events(self, request_id: str, params: dict[str, Any]) -> None:
        session_id = str(params.get("session_id") or "")
        after_sequence = int(params.get("after_sequence") or 0)
        limit = min(2_000, max(1, int(params.get("limit") or 2_000)))
        raw_event_types = params.get("event_types")
        if raw_event_types is not None and not (
            isinstance(raw_event_types, list)
            and all(isinstance(value, str) for value in raw_event_types)
        ):
            raise ValueError("event_types must be an array of strings")
        event_types = set(raw_event_types) if raw_event_types is not None else None
        if not session_id:
            raise ValueError("session_id must not be empty")
        runtime = self._require_runtime(params, session_id=session_id)
        project_id = self._runtime_project_id(runtime)
        emitter = self.project_emitters.get(project_id)
        if emitter is None:
            # Backward-compatible single-runtime embedding: older callers and
            # protocol tests attach the active journal directly to ``events``.
            emitter = self.events
        replayed = emitter.replay(
            session_id,
            after_sequence,
            limit=limit + 1,
            event_types=event_types,
        )
        has_more = len(replayed) > limit
        events = replayed[:limit]
        self.writer(
            response(
                request_id,
                result={
                    "events": events,
                    "last_sequence": (
                        int(events[-1].get("sequence") or after_sequence)
                        if events
                        else after_sequence
                    ),
                    "has_more": has_more,
                },
            )
        )

    def _install_mcp(self, request_id: str, params: dict[str, Any]) -> None:
        runtime = self._require_runtime(params)
        self._assert_idle(runtime)
        name = _bounded_string(params.get("name"), "name", 64)
        raw_config = params.get("config")
        if not isinstance(raw_config, dict):
            raise ValueError("config must be an object")
        command = _optional_bounded_string(raw_config.get("command"), "command", 2048)
        url = _optional_bounded_string(raw_config.get("url"), "url", 4096)
        if command and params.get("confirmed") is not True:
            raise ValueError("confirmed=true is required before starting an MCP command")
        disabled = raw_config.get("disabled", False)
        if not isinstance(disabled, bool):
            raise ValueError("config.disabled must be a boolean")
        config = McpServerConfig(
            command=command,
            args=_bounded_string_list(raw_config.get("args"), "args", 64, 4096),
            env=_bounded_string_map(raw_config.get("env"), "env", 64, 4096),
            url=url,
            headers=_bounded_string_map(
                raw_config.get("headers"),
                "headers",
                64,
                4096,
            ),
            disabled=disabled,
            source="project",
        )
        result = runtime.install_mcp_server(
            name,
            config,
            overwrite=bool(params.get("overwrite", False)),
        )
        self.writer(response(request_id, result=result))

    def _set_mcp_enabled(self, request_id: str, params: dict[str, Any]) -> None:
        runtime = self._require_runtime(params)
        self._assert_idle(runtime)
        name = _bounded_string(params.get("name"), "name", 64)
        enabled = params.get("enabled")
        if not isinstance(enabled, bool):
            raise ValueError("enabled must be a boolean")
        result = runtime.set_mcp_server_enabled(name, enabled)
        self.writer(response(request_id, result=result))

    def _restart_mcp(self, request_id: str, params: dict[str, Any]) -> None:
        runtime = self._require_runtime(params)
        self._assert_idle(runtime)
        name = _bounded_string(params.get("name"), "name", 64)
        result = runtime.restart_mcp_server(name)
        self.writer(response(request_id, result=result))

    def _remove_mcp(self, request_id: str, params: dict[str, Any]) -> None:
        runtime = self._require_runtime(params)
        self._assert_idle(runtime)
        name = _bounded_string(params.get("name"), "name", 64)
        result = runtime.remove_mcp_server(name)
        self.writer(response(request_id, result=result))

    def _mcp_logs(self, request_id: str, params: dict[str, Any]) -> None:
        runtime = self._require_runtime(params)
        name = _bounded_string(params.get("name"), "name", 64)
        self.writer(response(request_id, result=runtime.mcp_server_logs(name)))

    def _list_memory(self, request_id: str, params: dict[str, Any]) -> None:
        runtime = self._require_runtime(params)
        query = _optional_bounded_string(params.get("query"), "query", 2_048)
        limit = _bounded_int(params.get("limit", 200), "limit", 1, 500)
        self.writer(
            response(request_id, result=runtime.memory_snapshot(query=query, limit=limit))
        )

    def _save_memory(self, request_id: str, params: dict[str, Any]) -> None:
        runtime = self._require_runtime(params)
        self._assert_idle(runtime)
        content = _bounded_string(params.get("content"), "content", 10_000)
        self.writer(response(request_id, result=runtime.save_memory(content)))

    def _delete_memory(self, request_id: str, params: dict[str, Any]) -> None:
        runtime = self._require_runtime(params)
        self._assert_idle(runtime)
        entry_id = _bounded_string(params.get("id"), "id", 128)
        self.writer(response(request_id, result=runtime.delete_memory(entry_id)))

    def _clear_memory(self, request_id: str, params: dict[str, Any]) -> None:
        runtime = self._require_runtime(params)
        self._assert_idle(runtime)
        if params.get("confirmed") is not True:
            raise ValueError("confirmed=true is required before clearing project memory")
        self.writer(response(request_id, result=runtime.clear_memory()))

    def _list_skills(self, request_id: str, params: dict[str, Any]) -> None:
        runtime = self._require_runtime(params)
        self.writer(response(request_id, result=runtime.skill_snapshot()))

    def _get_skill(self, request_id: str, params: dict[str, Any]) -> None:
        runtime = self._require_runtime(params)
        name = _bounded_string(params.get("name"), "name", 128)
        self.writer(response(request_id, result=runtime.skill_detail(name)))

    def _skill_diff(self, request_id: str, params: dict[str, Any]) -> None:
        runtime = self._require_runtime(params)
        name = _bounded_string(params.get("name"), "name", 128)
        max_chars = _bounded_int(params.get("max_chars", 120_000), "max_chars", 1_000, 200_000)
        self.writer(response(request_id, result=runtime.skill_diff(name, max_chars)))

    def _set_skill_enabled(self, request_id: str, params: dict[str, Any]) -> None:
        runtime = self._require_runtime(params)
        self._assert_idle(runtime)
        name = _bounded_string(params.get("name"), "name", 128)
        enabled = params.get("enabled")
        if not isinstance(enabled, bool):
            raise ValueError("enabled must be a boolean")
        self.writer(
            response(request_id, result=runtime.set_skill_enabled(name, enabled))
        )

    def _reload_skills(self, request_id: str, params: dict[str, Any]) -> None:
        runtime = self._require_runtime(params)
        self._assert_idle(runtime)
        self.writer(response(request_id, result=runtime.reload_skills()))

    def _change_bundled_skill(
        self,
        request_id: str,
        params: dict[str, Any],
        action: str,
    ) -> None:
        runtime = self._require_runtime(params)
        self._assert_idle(runtime)
        if action in {"update", "restore_default"} and params.get("confirmed") is not True:
            raise ValueError(
                "confirmed=true is required before replacing a customized bundled Skill"
            )
        name = _bounded_string(params.get("name"), "name", 128)
        current_hash = _bounded_sha256(params.get("current_hash"), "current_hash")
        builtin_hash = _bounded_sha256(params.get("builtin_hash"), "builtin_hash")
        self.writer(
            response(
                request_id,
                result=runtime.update_bundled_skill(
                    name,
                    action=action,
                    expected_current_hash=current_hash,
                    expected_builtin_hash=builtin_hash,
                ),
            )
        )

    def _browser_snapshot(self, request_id: str, params: dict[str, Any]) -> None:
        runtime = self._require_runtime(params)
        self.writer(response(request_id, result=runtime.browser_snapshot()))

    def _probe_browser(self, request_id: str, params: dict[str, Any]) -> None:
        runtime = self._require_runtime(params)
        port = _bounded_int(params.get("port", 9222), "port", 1_024, 65_535)
        self.writer(response(request_id, result=runtime.probe_browser(port)))

    def _connect_browser(self, request_id: str, params: dict[str, Any]) -> None:
        runtime = self._require_runtime(params)
        self._assert_idle(runtime)
        if params.get("confirmed") is not True:
            raise ValueError("confirmed=true is required before connecting shared Chrome")
        raw_port = params.get("port")
        port = None if raw_port is None else _bounded_int(raw_port, "port", 1_024, 65_535)
        self.writer(response(request_id, result=runtime.connect_browser(port)))

    def _disconnect_browser(self, request_id: str, params: dict[str, Any]) -> None:
        runtime = self._require_runtime(params)
        self._assert_idle(runtime)
        if params.get("confirmed") is not True:
            raise ValueError("confirmed=true is required before disconnecting shared Chrome")
        self.writer(response(request_id, result=runtime.disconnect_browser()))

    def _browser_tabs(self, request_id: str, params: dict[str, Any]) -> None:
        runtime = self._require_runtime(params)
        self.writer(response(request_id, result=runtime.browser_tabs()))

    def _rag_snapshot(self, request_id: str, params: dict[str, Any]) -> None:
        runtime = self._require_runtime(params)
        self.writer(response(request_id, result=self._rag_snapshot_with_status(runtime)))

    def _add_rag_sources(self, request_id: str, params: dict[str, Any]) -> None:
        runtime = self._require_runtime(params)
        self._assert_idle(runtime)
        raw_paths = _bounded_string_list(params.get("paths"), "paths", 100, 4096)
        if not raw_paths:
            raise ValueError("paths must contain at least one file or folder")
        paths = [_validated_rag_source(path) for path in raw_paths]
        result = runtime.add_rag_sources(paths)
        self.writer(response(request_id, result={**result, "status": "idle"}))

    def _remove_rag_source(self, request_id: str, params: dict[str, Any]) -> None:
        runtime = self._require_runtime(params)
        self._assert_idle(runtime)
        path = _bounded_string(params.get("path"), "path", 4096)
        result = runtime.remove_rag_source(path)
        self.writer(response(request_id, result={**result, "status": "idle"}))

    def _start_rag_index(self, request_id: str, params: dict[str, Any]) -> None:
        runtime = self._require_runtime(params)
        self._assert_idle(runtime)
        if not runtime.rag_snapshot()["sources"]:
            raise ValueError("add at least one file or folder before building the RAG index")
        with self._rag_lock:
            job_id = f"rag-{uuid.uuid4().hex}"
            project_id = self._runtime_project_id(runtime)
            self.rag_jobs[project_id] = job_id
            if self.runtime is runtime:
                self.active_rag_job_id = job_id
        self.writer(response(request_id, result={
            **runtime.rag_snapshot(),
            "status": "indexing",
            "job_id": job_id,
        }))
        threading.Thread(
            target=self._run_rag_index,
            args=(runtime, job_id),
            name="stellarcode-rag-index",
            daemon=True,
        ).start()

    def _run_rag_index(self, runtime: RuntimeSession, job_id: str) -> None:
        project_id = self._runtime_project_id(runtime)
        session_id = f"workspace-{project_id}"
        self._emit_event(
            "rag.index.started",
            {"job_id": job_id, "source_count": runtime.rag_snapshot()["source_count"]},
            session_id=session_id,
        )
        try:
            snapshot = runtime.rebuild_rag_index(
                lambda message: self._emit_event(
                    "rag.index.progress",
                    {"job_id": job_id, "message": message},
                    session_id=session_id,
                )
            )
            self._finish_rag_job(project_id, job_id)
            self._emit_event(
                "rag.index.completed",
                {"job_id": job_id, "snapshot": {**snapshot, "status": "idle"}},
                session_id=session_id,
            )
        except Exception as exc:
            self._finish_rag_job(project_id, job_id)
            self._emit_event(
                "rag.index.failed",
                {"job_id": job_id, "message": f"{type(exc).__name__}: {exc}"},
                session_id=session_id,
            )
        finally:
            self._finish_rag_job(project_id, job_id)

    def _clear_rag_index(self, request_id: str, params: dict[str, Any]) -> None:
        runtime = self._require_runtime(params)
        self._assert_idle(runtime)
        if params.get("confirmed") is not True:
            raise ValueError("confirmed=true is required before clearing the RAG index")
        result = runtime.clear_rag_index()
        self.writer(response(request_id, result={**result, "status": "idle"}))

    def _rag_snapshot_with_status(self, runtime: RuntimeSession) -> dict[str, Any]:
        job_id = self.rag_jobs.get(self._runtime_project_id(runtime))
        return {
            **runtime.rag_snapshot(),
            "status": "indexing" if job_id else "idle",
            **({"job_id": job_id} if job_id else {}),
        }

    def _finish_rag_job(self, project_id: str, job_id: str) -> None:
        with self._rag_lock:
            if self.rag_jobs.get(project_id) == job_id:
                self.rag_jobs.pop(project_id, None)
            if self.project_id == project_id and self.active_rag_job_id == job_id:
                self.active_rag_job_id = None

    def _diagnostics_snapshot(self, request_id: str, params: dict[str, Any]) -> None:
        runtime = self._require_runtime(params)
        snapshot = runtime.diagnostics_snapshot()
        with self._diagnostics_lock:
            job_id = self.diagnostics_jobs.get(self._runtime_project_id(runtime))
        if job_id:
            snapshot = {
                **snapshot,
                "status": "running",
                "run_id": job_id,
                "progress": snapshot.get("progress") or "Workspace diagnostics are running.",
            }
        self.writer(response(request_id, result=snapshot))

    def _start_diagnostics(self, request_id: str, params: dict[str, Any]) -> None:
        runtime = self._require_runtime(params)
        self._assert_idle(runtime)
        profile = _bounded_choice(params.get("profile", "safe"), "profile", {"safe", "build"})
        if profile == "build" and params.get("confirmed") is not True:
            raise ValueError("confirmed=true is required before running build diagnostics")
        job_id = f"diagnostics-{uuid.uuid4().hex}"
        cancel_event = threading.Event()
        project_id = self._runtime_project_id(runtime)
        with self._diagnostics_lock:
            self.diagnostics_jobs[project_id] = job_id
            self.diagnostics_cancel_events[project_id] = cancel_event
            if self.runtime is runtime:
                self.active_diagnostics_job_id = job_id
                self._diagnostics_cancel_event = cancel_event
        initial = {
            **runtime.diagnostics_snapshot(),
            "status": "running",
            "run_id": job_id,
            "profile": profile,
            "progress": "Starting workspace diagnostics...",
            "error": None,
        }
        self.writer(response(request_id, result=initial))
        threading.Thread(
            target=self._run_diagnostics,
            args=(runtime, job_id, profile, cancel_event),
            name="stellarcode-workspace-diagnostics",
            daemon=True,
        ).start()

    def _run_diagnostics(
        self,
        runtime: RuntimeSession,
        job_id: str,
        profile: str,
        cancel_event: threading.Event,
    ) -> None:
        project_id = self._runtime_project_id(runtime)
        session_id = f"workspace-{project_id}"
        self._emit_event(
            "diagnostics.started",
            {"run_id": job_id, "profile": profile},
            session_id=session_id,
        )
        try:
            snapshot = runtime.run_diagnostics(
                profile,
                lambda message: self._emit_event(
                    "diagnostics.progress",
                    {"run_id": job_id, "message": str(message)},
                    session_id=session_id,
                ),
                cancel_event,
                run_id=job_id,
            )
            self._finish_diagnostics_job(project_id, job_id)
            status = str(snapshot.get("status") or "failed")
            if status == "cancelled":
                self._emit_event(
                    "diagnostics.cancelled",
                    {"run_id": job_id},
                    session_id=session_id,
                )
            elif status == "completed":
                self._emit_event(
                    "diagnostics.completed",
                    {
                        "run_id": job_id,
                        "error_count": int(snapshot.get("error_count") or 0),
                        "warning_count": int(snapshot.get("warning_count") or 0),
                        "information_count": int(snapshot.get("information_count") or 0),
                    },
                    session_id=session_id,
                )
            else:
                self._emit_event(
                    "diagnostics.failed",
                    {
                        "run_id": job_id,
                        "message": str(snapshot.get("error") or "Diagnostics failed."),
                    },
                    session_id=session_id,
                )
        except Exception as exc:
            self._finish_diagnostics_job(project_id, job_id)
            self._emit_event(
                "diagnostics.failed",
                {"run_id": job_id, "message": f"{type(exc).__name__}: {exc}"},
                session_id=session_id,
            )
        finally:
            self._finish_diagnostics_job(project_id, job_id)

    def _cancel_diagnostics(self, request_id: str, params: dict[str, Any]) -> None:
        run_id = _bounded_string(params.get("run_id"), "run_id", 128)
        runtime = self._require_runtime(params)
        project_id = self._runtime_project_id(runtime)
        with self._diagnostics_lock:
            cancel_event = self.diagnostics_cancel_events.get(project_id)
            if self.diagnostics_jobs.get(project_id) != run_id or cancel_event is None:
                self._error(request_id, "diagnostics_not_running", "diagnostics run is no longer active")
                return
            cancel_event.set()
        self.writer(
            response(
                request_id,
                result={
                    **runtime.diagnostics_snapshot(),
                    "status": "running",
                    "run_id": run_id,
                    "progress": "Cancelling workspace diagnostics...",
                },
            )
        )

    def _finish_diagnostics_job(self, project_id: str, job_id: str) -> None:
        with self._diagnostics_lock:
            if self.diagnostics_jobs.get(project_id) == job_id:
                self.diagnostics_jobs.pop(project_id, None)
                self.diagnostics_cancel_events.pop(project_id, None)
            if self.project_id == project_id and self.active_diagnostics_job_id == job_id:
                self.active_diagnostics_job_id = None
                self._diagnostics_cancel_event = None

    def _cancel_task(self, request_id: str, params: dict[str, Any]) -> None:
        task_id = str(params.get("task_id") or "")
        session_id = str(params.get("session_id") or "")
        if not task_id:
            raise ValueError("task_id must not be empty")
        runtime = self._require_runtime(params, session_id=session_id, task_id=task_id)
        # Keep the phase check, Runtime cancellation, and route transition in one
        # critical section. Otherwise terminal finalization can change the route
        # between these steps: cancel_task() would already set the cancellation
        # flag, then ``finalizing -> cancelling`` would raise an internal error.
        # Once finalization starts, its durable terminal intent is authoritative
        # and the task is no longer cancellable.
        with self._task_lock:
            route = self.task_routes.get(task_id)
            matches = (
                route is not None
                and route.runtime is runtime
                and (not session_id or route.session_id == session_id)
                and route.phase in {"accepted", "running"}
            )
            accepted = bool(matches and route and route.runtime.cancel_task(task_id))
            if accepted:
                self.task_state.transition(task_id, "cancelling")
        self.writer(
            response(
                request_id,
                result={"accepted": accepted, "task_id": task_id},
            )
        )

    def _rollback_task(self, request_id: str, params: dict[str, Any]) -> None:
        task_id = str(params.get("task_id") or "")
        session_id = str(params.get("session_id") or "")
        runtime = self._require_runtime(params, session_id=session_id, task_id=task_id)
        self._assert_idle(runtime)
        snapshot_id = str(params.get("snapshot_id") or "") or None
        if not task_id or not session_id:
            raise ValueError("task_id and session_id must not be empty")
        if params.get("confirmed") is not True:
            raise ValueError("confirmed=true is required before rolling back task changes")
        self._emit_event(
            "task.rollback.started",
            {"snapshot_id": snapshot_id or ""},
            session_id=session_id,
            task_id=task_id,
        )
        try:
            result = runtime.rollback_task_changes(task_id, session_id, snapshot_id)
        except WorkspaceRollbackConflict as exc:
            self._emit_event(
                "task.rollback.failed",
                {
                    "snapshot_id": snapshot_id or "",
                    "code": "workspace_changed",
                    "message": str(exc),
                    "conflicted_paths": exc.paths,
                },
                session_id=session_id,
                task_id=task_id,
            )
            self._error(request_id, "rollback_conflict", str(exc))
            return
        except WorkspaceProtectionError as exc:
            self._emit_event(
                "task.rollback.failed",
                {
                    "snapshot_id": snapshot_id or "",
                    "code": "rollback_unavailable",
                    "message": str(exc),
                    "conflicted_paths": [],
                },
                session_id=session_id,
                task_id=task_id,
            )
            # The protection record marks mutation-time failures as pending before
            # raising.  Clear that marker only after the failure event is durably in
            # the Runtime journal, so a crash cannot silently lose the warning.
            runtime.acknowledge_rollback_recovery(task_id)
            self._error(request_id, "rollback_unavailable", str(exc))
            return
        self._emit_event(
            "task.rollback.completed",
            result,
            session_id=session_id,
            task_id=task_id,
        )
        self.writer(response(request_id, result=result))

    def _task_diff(self, request_id: str, params: dict[str, Any]) -> None:
        task_id = str(params.get("task_id") or "")
        session_id = str(params.get("session_id") or "")
        runtime = self._require_runtime(params, session_id=session_id, task_id=task_id)
        self._assert_idle(runtime)
        if not task_id or not session_id:
            raise ValueError("task_id and session_id must not be empty")
        max_chars = min(200_000, max(1_000, int(params.get("max_chars") or 80_000)))
        result = runtime.task_change_diff(task_id, session_id, max_chars)
        self.writer(response(request_id, result=result))

    def _resolve_approval(self, request_id: str, params: dict[str, Any]) -> None:
        approval_id = str(params.get("approval_id") or "")
        decision = str(params.get("decision") or "")
        effective_arguments = params.get("effective_arguments")
        requested_session_id = str(params.get("session_id") or "")
        requested_task_id = str(params.get("task_id") or "")
        runtime: RuntimeSession | None = None
        requested_project_id = str(params.get("project_id") or "").strip()
        if requested_project_id or requested_session_id or requested_task_id:
            runtime = self._require_runtime(
                params,
                session_id=requested_session_id,
                task_id=requested_task_id,
            )
        if runtime is None:
            for candidate in self.runtimes.values():
                context_reader = getattr(candidate, "approval_context", None)
                if callable(context_reader) and context_reader(approval_id) is not None:
                    runtime = candidate
                    break
        if runtime is None:
            runtime = self._require_runtime(params)
        context_reader = getattr(runtime, "approval_context", None)
        context = context_reader(approval_id) if callable(context_reader) else None
        if callable(context_reader) and context is None:
            self._error(request_id, "approval_not_pending", "approval is no longer pending")
            return
        if context is None:
            project_id = self._runtime_project_id(runtime)
            session_id = (
                self.project_active_sessions.get(project_id)
                or (self.active_session_id if self.runtime is runtime else "")
                or requested_session_id
                or "runtime"
            )
            task_id = (
                self.session_tasks.get(session_id)
                or (self.active_task_id if self.runtime is runtime else None)
                or requested_task_id
            )
        else:
            session_id, task_id = context
        if requested_session_id and requested_session_id != session_id:
            self._error(request_id, "approval_context_mismatch", "approval session is not active")
            return
        if requested_task_id and requested_task_id != task_id:
            self._error(request_id, "approval_context_mismatch", "approval task is not active")
            return
        with self._task_lock:
            route = self.task_routes.get(str(task_id or ""))
        if route is not None and route.phase != "running":
            self._error(
                request_id,
                "approval_not_pending",
                f"approval cannot be resolved while task is {route.phase}",
            )
            return
        event_data = {
            "approval_id": approval_id,
            "decision": decision,
            **(
                {
                    "effective_arguments": runtime.safe_approval_arguments(
                        approval_id,
                        effective_arguments,
                    )
                }
                if effective_arguments is not None
                else {}
            ),
        }
        if not runtime.resolve_approval(
            approval_id,
            decision,
            effective_arguments,
            before_release=lambda: self._emit_event(
                "approval.resolved",
                event_data,
                session_id=session_id,
                task_id=task_id,
            ),
        ):
            self._error(request_id, "approval_not_pending", "approval is no longer pending")
            return
        self.writer(response(request_id, result={}))

    def _runtime_for_request(
        self,
        params: dict[str, Any] | None = None,
        *,
        session_id: str | None = None,
        task_id: str | None = None,
        strict: bool = True,
    ) -> RuntimeSession | None:
        """Resolve a request to its owning project without changing active UI aliases.

        ``project_id`` is authoritative when supplied. Otherwise an existing task
        route or the persisted ``session_projects`` ownership map selects the
        runtime. Falling back to ``self.runtime`` is kept only for the legacy
        single-workspace protocol and embedded unit tests.
        """

        values = params or {}
        requested_project_id = str(values.get("project_id") or "").strip()
        requested_session_id = str(
            session_id if session_id is not None else values.get("session_id") or ""
        ).strip()
        requested_task_id = str(
            task_id if task_id is not None else values.get("task_id") or ""
        ).strip()

        route = None
        if requested_task_id:
            with self._task_lock:
                route = self.task_routes.get(requested_task_id)
        session_project_id = self.session_projects.get(requested_session_id)

        if requested_project_id:
            runtime = self.runtimes.get(requested_project_id)
            if runtime is None and self.runtime is not None:
                if self._runtime_project_id(self.runtime) == requested_project_id:
                    runtime = self.runtime
            if runtime is None:
                if strict:
                    raise RuntimeError(f"project is not loaded: {requested_project_id}")
                return None
            if session_project_id and session_project_id != requested_project_id:
                if strict:
                    raise ValueError(
                        f"session {requested_session_id} belongs to project "
                        f"{session_project_id}, not {requested_project_id}"
                    )
                return None
            if route is not None and route.project_id != requested_project_id:
                if strict:
                    raise ValueError(
                        f"task {requested_task_id} belongs to project "
                        f"{route.project_id}, not {requested_project_id}"
                    )
                return None
            return runtime

        if route is not None:
            if session_project_id and session_project_id != route.project_id:
                if strict:
                    raise ValueError(
                        f"session {requested_session_id} belongs to project "
                        f"{session_project_id}, but task {requested_task_id} belongs to "
                        f"project {route.project_id}"
                    )
                return None
            if requested_session_id and route.session_id != requested_session_id:
                if strict:
                    raise ValueError(
                        f"task {requested_task_id} belongs to session "
                        f"{route.session_id}, not {requested_session_id}"
                    )
                return None
            return route.runtime

        if session_project_id:
            runtime = self.runtimes.get(session_project_id)
            if runtime is None and self.runtime is not None:
                if self._runtime_project_id(self.runtime) == session_project_id:
                    runtime = self.runtime
            if runtime is None:
                if strict:
                    raise RuntimeError(
                        f"project {session_project_id} for session "
                        f"{requested_session_id} is not loaded"
                    )
                return None
            return runtime

        if requested_session_id.startswith("workspace-"):
            workspace_project_id = requested_session_id.removeprefix("workspace-")
            runtime = self.runtimes.get(workspace_project_id)
            if runtime is not None:
                return runtime

        if requested_session_id or requested_task_id:
            # In a real multi-project process, silently using the currently
            # visible workspace would be a cross-project data leak. Legacy
            # single-runtime embeddings do not populate ``runtimes`` and retain
            # their historical fallback behavior.
            if len(self.runtimes) > 1:
                identifier = requested_session_id or requested_task_id
                if strict:
                    raise RuntimeError(
                        f"project for request context {identifier} is unknown; provide project_id"
                    )
                return None

        if self.runtime is None:
            if strict:
                raise RuntimeError("open a workspace first")
            return None
        return self.runtime

    def _require_runtime(
        self,
        params: dict[str, Any] | None = None,
        *,
        session_id: str | None = None,
        task_id: str | None = None,
    ) -> RuntimeSession:
        runtime = self._runtime_for_request(
            params,
            session_id=session_id,
            task_id=task_id,
        )
        assert runtime is not None
        return runtime

    def _session_id_for_runtime(
        self,
        params: dict[str, Any],
        runtime: RuntimeSession,
    ) -> str:
        requested = str(params.get("session_id") or "").strip()
        if requested:
            return requested
        project_id = self._runtime_project_id(runtime)
        return str(
            self.project_active_sessions.get(project_id)
            or (self.active_session_id if self.runtime is runtime else "")
            or ""
        )

    def _set_project_active_context(
        self,
        runtime: RuntimeSession,
        session_id: str,
        task_id: str | None,
    ) -> None:
        project_id = self._runtime_project_id(runtime)
        self.project_active_sessions[project_id] = session_id
        if self.runtime is runtime:
            self.active_session_id = session_id
            self.active_task_id = task_id

    def _assert_idle(self, runtime: RuntimeSession | None = None) -> None:
        target = runtime or self._require_runtime()
        project_id = self._runtime_project_id(target)
        if (
            self._project_has_active_tasks(project_id)
            or self.rag_jobs.get(project_id)
            or self.diagnostics_jobs.get(project_id)
        ):
            raise RuntimeError(
                "wait for the active task, RAG index, or diagnostics run to finish"
            )

    def _assert_session_idle(self, session_id: str) -> None:
        with self._task_lock:
            if session_id in self.session_tasks:
                raise RuntimeError("wait for this conversation task to finish")

    def _project_has_active_tasks(self, project_id: str) -> bool:
        return self.task_state.has_project_tasks(project_id)

    def _runtime_project_id(self, runtime: RuntimeSession) -> str:
        """Return a stable project id for full and legacy embedded runtimes."""

        return str(getattr(runtime, "project_id", None) or self.project_id or "runtime")

    def _register_task_route(
        self,
        runtime: RuntimeSession,
        session_id: str,
        task_id: str,
        *,
        phase: TaskRoutePhase = "accepted",
    ) -> None:
        project_id = self._runtime_project_id(runtime)
        self.task_state.register(
            runtime,
            project_id,
            session_id,
            task_id,
            phase=phase,
        )
        self.session_projects[session_id] = project_id

    def _release_task_route(self, task_id: str) -> None:
        self.task_state.release(task_id)
        if self.active_task_id == task_id:
            self.active_task_id = None

    def _runtime_for_event(
        self,
        session_id: str,
        task_id: str | None,
    ) -> RuntimeSession | None:
        if task_id:
            with self._task_lock:
                route = self.task_routes.get(task_id)
            if route is not None:
                return route.runtime
        project_id = self.session_projects.get(session_id)
        if project_id:
            return self.runtimes.get(project_id)
        if session_id.startswith("workspace-"):
            return self.runtimes.get(session_id.removeprefix("workspace-"))
        return self.runtime

    def _emit_event(
        self,
        event_type: str,
        data: dict[str, Any],
        *,
        session_id: str = "runtime",
        task_id: str | None = None,
    ) -> dict[str, Any]:
        runtime = self._runtime_for_event(session_id, task_id)
        if runtime is not None:
            runtime.record_runtime_event(event_type, data, session_id, task_id)
        runtime_project_id = getattr(runtime, "project_id", None)
        emitter = (
            self.project_emitters.get(runtime_project_id)
            if runtime_project_id
            else None
        ) or self.events
        return emitter.emit(
            event_type,
            data,
            session_id=session_id,
            task_id=task_id,
        )

    def _emit_task_progress_best_effort(
        self,
        event_type: str,
        data: dict[str, Any],
        *,
        session_id: str,
        task_id: str,
    ) -> None:
        """Publish a non-terminal task UI event without owning task outcome.

        The conversation/checkpoint and terminal EventJournal entry are the
        durable sources of truth. A trace, journal, or desktop transport error
        while publishing progress must not skip Agent execution, finalization,
        or convert a completed task into a failed one.
        """

        try:
            self._emit_event(
                event_type,
                data,
                session_id=session_id,
                task_id=task_id,
            )
        except Exception as exc:
            print(
                f"StellarCode could not publish {event_type} for {task_id}: "
                f"{type(exc).__name__}: {exc}",
                file=sys.stderr,
                flush=True,
            )

    def _error(self, request_id: str, code: str, message: str) -> None:
        self.writer(response(request_id, error_code=code, error_message=message))


def create_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="StellarCode JSONL runtime sidecar")
    parser.add_argument("--workspace", default=".")
    parser.add_argument("--data-dir", default=None)
    parser.add_argument("--worktree-dir", default=None)
    parser.add_argument("--max-iterations", type=int, default=8)
    parser.add_argument("--max-parallel-tools", type=int, default=4)
    parser.add_argument("--tool-batch-timeout", type=float, default=90)
    parser.add_argument("--plan-workers", type=int, default=4)
    parser.add_argument("--team-workers", type=int, default=2)
    parser.add_argument("--team-retries", type=int, default=2)
    parser.add_argument("--context-window", type=int, default=200_000)
    parser.add_argument(
        "--rag-auto-retrieval",
        choices=("true", "false"),
        default="true",
    )
    parser.add_argument(
        "--diagnostics-lsp-enabled",
        choices=("true", "false"),
        default="false",
    )
    parser.add_argument("--diagnostics-lsp-command", default="")
    parser.add_argument("--diagnostics-lsp-args-json", default="[]")
    parser.add_argument("--diagnostics-lsp-timeout", type=float, default=20.0)
    return parser


def main() -> None:
    args = create_parser().parse_args()
    try:
        raw_lsp_args = json.loads(args.diagnostics_lsp_args_json)
    except json.JSONDecodeError as exc:
        raise SystemExit(f"invalid --diagnostics-lsp-args-json: {exc}") from exc
    if not isinstance(raw_lsp_args, list) or not all(
        isinstance(item, str) for item in raw_lsp_args
    ):
        raise SystemExit("--diagnostics-lsp-args-json must be a JSON array of strings")
    if len(raw_lsp_args) > 32 or any("\0" in item or len(item) > 2_048 for item in raw_lsp_args):
        raise SystemExit("--diagnostics-lsp-args-json exceeds the safe argv budget")
    writer = JsonLineWriter(sys.stdout)
    server = SidecarServer(
        Path(args.workspace),
        writer,
        data_dir=Path(args.data_dir) if args.data_dir else None,
        worktree_dir=Path(args.worktree_dir) if args.worktree_dir else None,
        max_iterations=args.max_iterations,
        max_parallel_tools=args.max_parallel_tools,
        tool_batch_timeout=args.tool_batch_timeout,
        plan_workers=args.plan_workers,
        team_workers=args.team_workers,
        team_retries=args.team_retries,
        context_window=args.context_window,
        rag_auto_retrieval=args.rag_auto_retrieval == "true",
        diagnostics_lsp_enabled=args.diagnostics_lsp_enabled == "true",
        diagnostics_lsp_command=args.diagnostics_lsp_command,
        diagnostics_lsp_args=tuple(raw_lsp_args),
        diagnostics_lsp_timeout=args.diagnostics_lsp_timeout,
    )
    server.start()
    try:
        for line in sys.stdin:
            try:
                request = json.loads(line)
                if not isinstance(request, dict):
                    raise ValueError("message must be a JSON object")
            except Exception as exc:
                writer(response("", error_code="invalid_message", error_message=str(exc)))
                continue
            if not server.handle(request):
                break
    finally:
        server.close()


def _desktop_visible_recoveries(
    recoveries: list[dict[str, Any]],
    active_task_ids: set[str],
) -> list[dict[str, Any]]:
    """Keep finalization state visible while hiding already-running Agent recovery.

    A finalize-pending task is registered as an active route so the Sidecar can
    finish its POST snapshot in the background.  The desktop must still receive
    that recovery item; otherwise replay concludes that no recovery exists,
    unlocks the composer, and the next submit receives a misleading task_busy.
    """

    return [
        recovery
        for recovery in recoveries
        if str(recovery.get("status") or "") == "finalize_pending"
        or str(recovery.get("task_id") or "") not in active_task_ids
    ]


def _fallback_project_id(workspace: Path) -> str:
    digest = hashlib.sha256(str(workspace).lower().encode("utf-8")).hexdigest()[:16]
    return f"project-{digest}"


def _bounded_string(value: object, field: str, maximum: int) -> str:
    result = _optional_bounded_string(value, field, maximum)
    if not result:
        raise ValueError(f"{field} must not be empty")
    return result


def _bounded_int(value: object, field: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{field} must be an integer")
    try:
        result = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be an integer") from exc
    if result < minimum or result > maximum:
        raise ValueError(f"{field} must be between {minimum} and {maximum}")
    return result


def _bounded_sha256(value: object, field: str) -> str:
    result = _bounded_string(value, field, 64).lower()
    if len(result) != 64 or any(character not in "0123456789abcdef" for character in result):
        raise ValueError(f"{field} must be a 64-character SHA-256 hex digest")
    return result


def _bounded_choice(value: object, field: str, choices: set[str]) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a string")
    result = value.strip().lower()
    if result not in choices:
        raise ValueError(f"{field} must be one of: {', '.join(sorted(choices))}")
    return result


def _optional_bounded_string(value: object, field: str, maximum: int) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a string")
    result = value.strip()
    if len(result) > maximum:
        raise ValueError(f"{field} must not exceed {maximum} characters")
    return result


def _bounded_string_list(
    value: object,
    field: str,
    maximum_items: int,
    maximum_length: int,
) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError(f"{field} must be an array of strings")
    if len(value) > maximum_items:
        raise ValueError(f"{field} must not contain more than {maximum_items} entries")
    if any(len(item) > maximum_length for item in value):
        raise ValueError(f"{field} entries must not exceed {maximum_length} characters")
    return list(value)


def _bounded_string_map(
    value: object,
    field: str,
    maximum_items: int,
    maximum_length: int,
) -> dict[str, str]:
    if value is None:
        return {}
    if not isinstance(value, dict) or not all(
        isinstance(key, str) and isinstance(item, str) for key, item in value.items()
    ):
        raise ValueError(f"{field} must map strings to strings")
    if len(value) > maximum_items:
        raise ValueError(f"{field} must not contain more than {maximum_items} entries")
    if any(
        not key.strip() or len(key) > 128 or len(item) > maximum_length
        for key, item in value.items()
    ):
        raise ValueError(
            f"{field} keys must contain 1-128 characters and values must not exceed "
            f"{maximum_length} characters"
        )
    return dict(value)


def _validated_rag_source(value: str) -> Path:
    path = Path(value).expanduser().resolve()
    if not path.exists():
        raise ValueError(f"RAG source does not exist: {path}")
    if not path.is_file() and not path.is_dir():
        raise ValueError(f"RAG source must be a file or directory: {path}")
    if path.is_file() and not is_indexable_file(path):
        raise ValueError(f"unsupported RAG source file type: {path.suffix or '<none>'}")
    if path.is_dir() and path.parent == path:
        raise ValueError("a filesystem root cannot be used as a RAG source")
    return path


if __name__ == "__main__":
    main()
