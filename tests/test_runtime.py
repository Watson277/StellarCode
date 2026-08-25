from __future__ import annotations

import json
import threading
import time
from io import StringIO
from types import SimpleNamespace

import pytest

from stellarcode.cancellation import TaskCancelledError
from stellarcode.hitl import ApprovalRequest, Decision
from stellarcode.llm.types import llm_runtime_scope
from stellarcode.memory import ProjectMemoryService
from stellarcode.runtime.core import (
    RuntimeSession,
    _record_plan_event,
    _task_workspace_prompt_context,
    _transcript_entry,
)
from stellarcode.runtime.hitl import RuntimeHitlHandler, task_approval_scope
from stellarcode.runtime.protocol import (
    PROTOCOL_VERSION,
    JsonLineWriter,
    RuntimeEventEmitter,
    response,
)
from stellarcode.runtime.recovery import EventJournal, TaskCheckpointStore
from stellarcode.runtime.sidecar import (
    SidecarServer,
    _desktop_visible_recoveries,
    create_parser,
)
from stellarcode.trace import TraceRecorder


def test_transcript_entry_persists_task_identity_for_assistant_answers():
    entry = _transcript_entry("assistant", "done", task_id="task-one")

    assert entry["role"] == "assistant"
    assert entry["content"] == "done"
    assert entry["task_id"] == "task-one"


def test_task_workspace_prompt_context_distinguishes_canonical_and_ephemeral_paths(
    tmp_path,
):
    project = tmp_path / "project"
    worktree = tmp_path / "runtime" / "w" / "abcd1234"

    context = _task_workspace_prompt_context(project, worktree)

    assert (
        f"canonical_project_root={json.dumps(str(project.resolve()), ensure_ascii=False)}"
        in context
    )
    assert (
        f"ephemeral_task_worktree={json.dumps(str(worktree.resolve()), ensure_ascii=False)}"
        in context
    )
    assert "never ephemeral_task_worktree" in context
    assert "Do not start detached/background processes" in context


def test_runtime_conversations_share_only_project_long_term_memory(tmp_path):
    runtime = object.__new__(RuntimeSession)
    runtime.project_memory = ProjectMemoryService(tmp_path / "memory")
    runtime.llm_client = object()
    runtime.registry = object()
    runtime.skill_registry = None
    runtime.skill_context_buffer = None
    runtime.workspace = tmp_path
    runtime.settings = SimpleNamespace(
        context_window=100_000,
        max_iterations=2,
        plan_workers=1,
        team_workers=1,
        team_retries=0,
        rag_auto_retrieval=False,
    )

    def create(identifier: str):
        return runtime._new_conversation(
            identifier,
            title=identifier,
            mode="react",
            created_at="2026-08-11T00:00:00+00:00",
            updated_at="2026-08-11T00:00:00+00:00",
            title_is_custom=False,
            trace_enabled=False,
            trace_path=None,
            transcript=[],
        )

    first = create("conversation-one")
    second = create("conversation-two")

    assert first.memory_manager is first.agent.memory_manager
    assert first.memory_manager is first.plan_agent.memory_manager
    assert first.memory_manager is first.team_agent.memory_manager
    assert first.agent.rag_auto_retrieval is False
    assert first.plan_agent.rag_auto_retrieval is False
    assert first.team_agent.rag_auto_retrieval is False
    assert first.team_agent.workers[0].rag_auto_retrieval is False
    assert first.memory_manager is not second.memory_manager
    assert first.memory_manager.short_term is not second.memory_manager.short_term
    assert first.memory_manager.token_budget is not second.memory_manager.token_budget
    assert first.memory_manager.long_term is second.memory_manager.long_term

    first.memory_manager.save_fact("All conversations use the same project formatter")

    assert [entry.content for entry in second.memory_manager.search("project formatter")] == [
        "All conversations use the same project formatter"
    ]


def test_task_approval_scope_is_isolated_between_concurrent_conversations():
    handler = RuntimeHitlHandler(lambda _event_type, _data: None, enabled=True)
    barrier = threading.Barrier(2)
    results: dict[str, bool] = {}

    def observe(name: str, access_mode: str) -> None:
        with task_approval_scope(access_mode):
            barrier.wait(timeout=2)
            results[name] = handler.is_enabled()

    restricted = threading.Thread(target=observe, args=("restricted", "restricted"))
    full_access = threading.Thread(target=observe, args=("full", "full-access"))
    restricted.start()
    full_access.start()
    restricted.join(timeout=2)
    full_access.join(timeout=2)

    assert results == {"restricted": True, "full": False}
    assert handler.is_enabled() is True


def test_desktop_keeps_active_finalize_pending_recovery_visible():
    recoveries = [
        {"task_id": "task-running", "status": "running"},
        {"task_id": "task-finalizing", "status": "finalize_pending"},
        {"task_id": "task-interrupted", "status": "prepared"},
    ]

    visible = _desktop_visible_recoveries(
        recoveries,
        {"task-running", "task-finalizing"},
    )

    assert [item["task_id"] for item in visible] == [
        "task-finalizing",
        "task-interrupted",
    ]


def test_runtime_event_emitter_orders_each_session_independently():
    messages: list[dict] = []
    emitter = RuntimeEventEmitter(messages.append)

    emitter.emit("runtime.ready", {"runtime_version": "test", "capabilities": []})
    emitter.emit("session.created", {"workspace": "x", "mode": "react"}, session_id="s1")
    emitter.emit(
        "task.started",
        {"mode": "react", "prompt_preview": "hi"},
        session_id="s1",
        task_id="t1",
    )

    assert [message["sequence"] for message in messages] == [1, 1, 2]
    assert all(message["protocol_version"] == PROTOCOL_VERSION for message in messages)
    assert messages[-1]["task_id"] == "t1"


def test_sidecar_parser_accepts_structured_lsp_configuration():
    args = create_parser().parse_args(
        [
            "--diagnostics-lsp-enabled",
            "true",
            "--diagnostics-lsp-command",
            r"C:\\Tools\\python-lsp.exe",
            "--diagnostics-lsp-args-json",
            '["--stdio", "--log-level=warning"]',
            "--diagnostics-lsp-timeout",
            "30",
        ]
    )

    assert args.diagnostics_lsp_enabled == "true"
    assert args.diagnostics_lsp_command == r"C:\\Tools\\python-lsp.exe"
    assert json.loads(args.diagnostics_lsp_args_json) == ["--stdio", "--log-level=warning"]
    assert args.diagnostics_lsp_timeout == 30


def test_sidecar_parser_accepts_custom_worktree_directory():
    args = create_parser().parse_args(["--worktree-dir", r"E:\\StellarCodeTemp"])

    assert args.worktree_dir == r"E:\\StellarCodeTemp"


def test_runtime_event_journal_continues_sequences_and_replays_after_restart(tmp_path):
    path = tmp_path / "events.jsonl"
    first_messages: list[dict] = []
    first = RuntimeEventEmitter(first_messages.append, EventJournal(path))
    first.emit("session.opened", {"title": "demo"}, session_id="session-one")
    first.emit(
        "tool.started",
        {
            "tool_call_id": "call-one",
            "name": "write_file",
            "arguments": {"path": "demo.txt"},
            "iteration": 1,
        },
        session_id="session-one",
        task_id="task-one",
    )

    second_messages: list[dict] = []
    second = RuntimeEventEmitter(second_messages.append, EventJournal(path))
    second.emit(
        "tool.failed",
        {
            "tool_call_id": "call-one",
            "name": "write_file",
            "error": "interrupted",
            "elapsed_ms": 0,
            "timed_out": False,
        },
        session_id="session-one",
        task_id="task-one",
    )

    replayed = second.replay("session-one", 1)

    assert [event["sequence"] for event in replayed] == [2, 3]
    assert [event["type"] for event in replayed] == ["tool.started", "tool.failed"]
    assert second_messages[0]["sequence"] == 3


def test_runtime_event_replay_can_filter_to_persisted_execution_details(tmp_path):
    path = tmp_path / "events.jsonl"
    emitter = RuntimeEventEmitter(lambda _message: None, EventJournal(path))
    emitter.emit(
        "assistant.delta",
        {"text": "large streamed answer"},
        session_id="session-one",
        task_id="task-one",
    )
    emitter.emit(
        "tool.started",
        {
            "tool_call_id": "call-one",
            "name": "read_file",
            "arguments": {"path": "demo.py"},
            "iteration": 1,
        },
        session_id="session-one",
        task_id="task-one",
    )
    emitter.emit(
        "task.completed",
        {"status": "completed", "elapsed_ms": 42},
        session_id="session-one",
        task_id="task-one",
    )

    replayed = emitter.replay(
        "session-one",
        0,
        event_types={"tool.started", "task.completed"},
    )

    assert [event["type"] for event in replayed] == [
        "tool.started",
        "task.completed",
    ]
    assert [event["sequence"] for event in replayed] == [2, 3]


def test_runtime_event_replay_skips_records_with_an_invalid_sequence(tmp_path):
    path = tmp_path / "events.jsonl"
    valid = {
        "kind": "event",
        "session_id": "session-one",
        "sequence": 1,
        "type": "tool.started",
    }
    invalid = {**valid, "sequence": "not-a-number", "type": "tool.failed"}
    path.write_text(
        "\n".join(json.dumps(item) for item in (valid, invalid)) + "\n",
        encoding="utf-8",
    )

    replayed = EventJournal(path).replay("session-one", 0)

    assert replayed == [valid]


def test_event_journal_repairs_a_crash_tail_before_committing_a_terminal_event(
    tmp_path,
):
    path = tmp_path / "events.jsonl"
    started = {
        "kind": "event",
        "session_id": "session-one",
        "sequence": 1,
        "task_id": "task-one",
        "type": "task.started",
        "data": {},
    }
    path.write_bytes(
        json.dumps(started, separators=(",", ":")).encode("utf-8")
        + b"\n"
        + b'{"kind":"event","session_id":'
    )

    journal = EventJournal(path)
    terminal = {
        "kind": "event",
        "session_id": "session-one",
        "sequence": 2,
        "task_id": "task-one",
        "type": "task.completed",
        "data": {"status": "completed"},
    }
    journal.append(terminal)

    assert journal.task_is_terminal("task-one") is True
    restarted = EventJournal(path)
    assert restarted.task_is_terminal("task-one") is True
    assert [event["type"] for event in restarted.replay("session-one", 0)] == [
        "task.started",
        "task.completed",
    ]


def test_event_journal_preserves_a_complete_json_tail_missing_only_its_newline(
    tmp_path,
):
    path = tmp_path / "events.jsonl"
    terminal = {
        "kind": "event",
        "session_id": "session-one",
        "sequence": 1,
        "task_id": "task-one",
        "type": "task.completed",
        "data": {"status": "completed"},
    }
    path.write_bytes(json.dumps(terminal, separators=(",", ":")).encode("utf-8"))

    journal = EventJournal(path)

    assert path.read_bytes().endswith(b"\n")
    assert journal.task_is_terminal("task-one") is True
    assert journal.replay("session-one", 0) == [terminal]


def test_sidecar_filtered_replay_reports_an_exact_next_page(tmp_path):
    messages: list[dict] = []
    server = SidecarServer(tmp_path, messages.append)
    server.runtime = SimpleNamespace()
    server.events.attach_journal(EventJournal(tmp_path / "events.jsonl"))
    for index in range(3):
        server.events.emit(
            "tool.started",
            {
                "tool_call_id": f"call-{index}",
                "name": "read_file",
                "arguments": {"path": f"demo-{index}.py"},
                "iteration": index + 1,
            },
            session_id="session-one",
            task_id="task-one",
        )
    messages.clear()

    server._replay_events(
        "replay-one",
        {
            "session_id": "session-one",
            "after_sequence": 0,
            "limit": 2,
            "event_types": ["tool.started"],
        },
    )
    first_page = messages.pop()["result"]
    server._replay_events(
        "replay-two",
        {
            "session_id": "session-one",
            "after_sequence": first_page["last_sequence"],
            "limit": 2,
            "event_types": ["tool.started"],
        },
    )
    second_page = messages.pop()["result"]

    assert [event["sequence"] for event in first_page["events"]] == [1, 2]
    assert first_page["has_more"] is True
    assert [event["sequence"] for event in second_page["events"]] == [3]
    assert second_page["has_more"] is False


@pytest.mark.parametrize(
    ("failure", "terminal_type"),
    [
        (TaskCancelledError("cancelled"), "task.cancelled"),
        (RuntimeError("failed"), "task.failed"),
    ],
)
def test_sidecar_terminal_failure_events_include_elapsed_time(
    tmp_path,
    failure,
    terminal_type,
):
    events: list[tuple[str, dict]] = []
    completed_checkpoints: list[str] = []
    server = SidecarServer(tmp_path, lambda _message: None)
    server.active_task_id = "task-one"
    server._emit_event = lambda event_type, data, **_kwargs: events.append(  # type: ignore[method-assign]
        (event_type, data)
    )
    terminal_state = {"committed": False}
    captured_terminal_data: dict = {}

    def finalize_pending(_task_id, _session_id):
        return terminal_type.removeprefix("task."), dict(captured_terminal_data)

    def mark_pending(_task_id, _outcome, data, *_args):
        captured_terminal_data.clear()
        captured_terminal_data.update(data)

    runtime = SimpleNamespace(
        pending_recovery=lambda: None,
        get_mode=lambda _session_id: "react",
        run=lambda *_args, **_kwargs: (_ for _ in ()).throw(failure),
        event_journal=SimpleNamespace(
            task_is_terminal=lambda _task_id: terminal_state["committed"],
        ),
        mark_task_finalize_pending=mark_pending,
        finalize_pending_task=finalize_pending,
        complete_task_checkpoint=completed_checkpoints.append,
    )

    original_emit = server._emit_event

    def emit_and_commit(event_type, data, **kwargs):
        original_emit(event_type, data, **kwargs)
        if event_type == terminal_type:
            terminal_state["committed"] = True

    server._emit_event = emit_and_commit  # type: ignore[method-assign]

    server._run_task(
        runtime,
        "session-one",
        "task-one",
        "test prompt",
    )

    terminal = next(data for event_type, data in events if event_type == terminal_type)
    assert isinstance(terminal["elapsed_ms"], int)
    assert terminal["elapsed_ms"] >= 0
    assert completed_checkpoints == ["task-one"]


@pytest.mark.parametrize(
    "failed_progress_event",
    [
        "task.started",
        "assistant.thinking:started",
        "assistant.completed",
        "assistant.thinking:finished",
    ],
)
def test_task_progress_delivery_failure_does_not_change_a_completed_outcome(
    tmp_path,
    failed_progress_event,
):
    events: list[str] = []
    marked_outcomes: list[str] = []
    completed_checkpoints: list[str] = []
    terminal_state = {"committed": False}
    run_calls = 0
    terminal_data: dict = {}
    server = SidecarServer(tmp_path, lambda _message: None)
    server.active_task_id = "task-one"

    def run(*_args, **_kwargs):
        nonlocal run_calls
        run_calls += 1
        return "successful answer"

    def mark_pending(_task_id, outcome, data, *_args):
        marked_outcomes.append(outcome)
        terminal_data.clear()
        terminal_data.update(data)

    runtime = SimpleNamespace(
        pending_recovery=lambda: None,
        get_mode=lambda _session_id: "react",
        run=run,
        event_journal=SimpleNamespace(
            task_is_terminal=lambda _task_id: terminal_state["committed"],
        ),
        mark_task_finalize_pending=mark_pending,
        finalize_pending_task=lambda _task_id, _session_id: (
            "completed",
            dict(terminal_data),
        ),
        complete_task_checkpoint=completed_checkpoints.append,
    )

    def failing_progress_emit(event_type, data, **_kwargs):
        marker = (
            f"{event_type}:{data.get('status')}"
            if event_type == "assistant.thinking"
            else event_type
        )
        events.append(marker)
        if marker == failed_progress_event:
            raise OSError("simulated progress transport failure")
        if event_type == "task.completed":
            terminal_state["committed"] = True

    server._emit_event = failing_progress_emit  # type: ignore[method-assign]

    server._run_task(runtime, "session-one", "task-one", "test prompt")

    assert run_calls == 1
    assert marked_outcomes == ["completed"]
    assert "task.completed" in events
    assert "task.failed" not in events
    assert completed_checkpoints == ["task-one"]
    assert server.active_task_id is None


def test_task_checkpoint_store_updates_and_clears_only_the_matching_task(tmp_path):
    store = TaskCheckpointStore(tmp_path / "recovery" / "active-task.json")
    store.write({"task_id": "task-one", "status": "prepared"})

    assert store.update("task-other", status="running") is None
    assert store.load()["status"] == "prepared"
    assert store.update("task-one", status="running")["status"] == "running"
    assert store.clear("task-other") is False
    assert store.clear("task-one") is True
    assert store.load() is None


def test_task_checkpoint_store_persists_multiple_conversation_tasks(tmp_path):
    store = TaskCheckpointStore(tmp_path / "recovery" / "active-task.json")
    store.write({"task_id": "task-one", "session_id": "session-one", "status": "running"})
    store.write({"task_id": "task-two", "session_id": "session-two", "status": "prepared"})

    assert {item["task_id"] for item in store.load_all()} == {"task-one", "task-two"}
    assert store.update("task-one", status="answer_ready")["status"] == "answer_ready"
    assert store.load("task-two")["status"] == "prepared"
    assert store.clear("task-one") is True
    assert store.load("task-one") is None
    assert store.load("task-two")["session_id"] == "session-two"


def test_pending_finalization_reuses_the_persisted_post_snapshot(tmp_path):
    runtime = object.__new__(RuntimeSession)
    runtime.task_checkpoints = TaskCheckpointStore(tmp_path / "active-task.json")
    runtime.task_checkpoints.write(
        {
            "task_id": "task-finalize",
            "session_id": "session-one",
            "status": "finalize_pending",
            "checkpoint_stage": "terminal_intent_recorded",
            "terminal_outcome": "completed",
            "terminal_data": {"status": "completed", "elapsed_ms": 42},
        }
    )
    calls: list[tuple[str, str]] = []
    runtime.finalize_task_changes = lambda task_id, outcome: (  # type: ignore[method-assign]
        calls.append((task_id, outcome))
        or {"task_id": task_id, "status": outcome, "changed_files": []}
    )

    first = runtime.finalize_pending_task("task-finalize", "session-one")
    second = runtime.finalize_pending_task("task-finalize", "session-one")

    assert first == second
    assert first[1]["elapsed_ms"] == 42
    assert first[1]["changes"]["task_id"] == "task-finalize"
    assert calls == [("task-finalize", "completed")]
    assert runtime.task_checkpoints.load()["checkpoint_stage"] == "workspace_finalized"


def test_worktree_merge_conflict_converts_completed_intent_to_failed(tmp_path):
    runtime = object.__new__(RuntimeSession)
    runtime.task_checkpoints = TaskCheckpointStore(tmp_path / "active-task.json")
    runtime.task_checkpoints.write(
        {
            "task_id": "task-conflict",
            "session_id": "session-one",
            "status": "finalize_pending",
            "checkpoint_stage": "terminal_intent_recorded",
            "terminal_outcome": "completed",
            "terminal_data": {"status": "completed", "elapsed_ms": 42},
            "answer": "Successfully wrote guide.md",
        }
    )
    saved: list[list[dict]] = []
    memory_messages: list[str] = []
    conversation = SimpleNamespace(
        transcript=[
            {"role": "user", "content": "write guide"},
            {"role": "assistant", "content": "Successfully wrote guide.md"},
        ],
        agent=SimpleNamespace(messages=[]),
        memory_manager=SimpleNamespace(add_assistant_message=memory_messages.append),
        updated_at="",
    )
    runtime.conversations = {"session-one": conversation}
    runtime._save_conversation = lambda value: saved.append(list(value.transcript))
    runtime.finalize_task_changes = lambda _task_id, _outcome: {  # type: ignore[method-assign]
        "task_id": "task-conflict",
        "status": "failed",
        "merge_conflict": True,
        "error": "same lines changed; worktree retained",
        "changed_files": [],
    }

    outcome, terminal = runtime.finalize_pending_task("task-conflict", "session-one")

    assert outcome == "failed"
    assert terminal["status"] == "failed"
    assert terminal["error_code"] == "worktree_merge_conflict"
    assert terminal["recoverable"] is False
    assert "worktree retained" in terminal["message"]
    checkpoint = runtime.task_checkpoints.load("task-conflict")
    assert checkpoint is not None
    assert checkpoint["terminal_outcome"] == "failed"
    assert conversation.transcript == [{"role": "user", "content": "write guide"}]
    assert "were not applied" in conversation.agent.messages[-1]["content"]
    assert "were not applied" in memory_messages[-1]
    assert saved[-1] == conversation.transcript


def test_terminal_journal_is_commit_point_even_if_transport_fails(tmp_path):
    journal = EventJournal(tmp_path / "events.jsonl")
    completed: list[str] = []

    def failing_writer(_message: dict) -> None:
        raise OSError("desktop transport closed")

    server = SidecarServer(tmp_path, failing_writer)
    server.events.attach_journal(journal)
    runtime = SimpleNamespace(
        event_journal=journal,
        complete_task_checkpoint=completed.append,
        record_runtime_event=lambda *_args, **_kwargs: None,
    )
    server.runtime = runtime

    committed = server._commit_terminal_event(
        runtime,
        "session-one",
        "task-one",
        "task.completed",
        {"status": "completed", "elapsed_ms": 1},
    )

    assert committed is True
    assert journal.task_is_terminal("task-one") is True
    assert completed == ["task-one"]
    assert len(journal.replay("session-one", 0)) == 1


def test_runtime_response_success_and_error_shapes():
    success = response("r1", result={"value": 1})
    failure = response("r2", error_code="BAD_REQUEST", error_message="invalid")

    assert success == {
        "kind": "response",
        "protocol_version": 1,
        "request_id": "r1",
        "ok": True,
        "result": {"value": 1},
    }
    assert failure["ok"] is False
    assert failure["error"]["code"] == "BAD_REQUEST"


def test_sidecar_accepts_desktop_agent_runtime_settings():
    args = create_parser().parse_args(
        [
            "--max-iterations",
            "12",
            "--max-parallel-tools",
            "6",
            "--tool-batch-timeout",
            "120",
            "--plan-workers",
            "5",
            "--team-workers",
            "3",
            "--team-retries",
            "4",
            "--context-window",
            "128000",
            "--rag-auto-retrieval",
            "false",
        ]
    )

    assert args.max_iterations == 12
    assert args.max_parallel_tools == 6
    assert args.tool_batch_timeout == 120
    assert args.plan_workers == 5
    assert args.team_workers == 3
    assert args.team_retries == 4
    assert args.context_window == 128000
    assert args.rag_auto_retrieval == "false"


def test_plan_runtime_events_persist_live_step_state_in_transcript():
    transcript: list[dict] = []
    created = {
        "goal": "build feature",
        "summary": "implement and verify",
        "execution_order": ["task_1", "task_2"],
        "tasks": [
            {
                "id": "task_1",
                "description": "implement",
                "task_type": "FILE_WRITE",
                "dependencies": [],
            },
            {
                "id": "task_2",
                "description": "verify",
                "task_type": "VERIFICATION",
                "dependencies": ["task_1"],
            },
        ],
    }

    assert _record_plan_event(transcript, "plan.created", created, "runtime-task")
    assert _record_plan_event(
        transcript,
        "plan.step.started",
        {"step_id": "task_1"},
        "runtime-task",
    )
    assert _record_plan_event(
        transcript,
        "plan.step.completed",
        {"step_id": "task_1", "result_preview": "done"},
        "runtime-task",
    )
    assert _record_plan_event(
        transcript,
        "plan.step.skipped",
        {"step_id": "task_2", "reason": "blocked"},
        "runtime-task",
    )

    assert transcript[0]["role"] == "plan"
    plan = transcript[0]["plan"]
    assert plan["task_id"] == "runtime-task"
    assert plan["steps"][0]["status"] == "completed"
    assert plan["steps"][0]["result_preview"] == "done"
    assert plan["steps"][1]["status"] == "skipped"
    assert plan["steps"][1]["error"] == "blocked"


def test_plan_recovery_keeps_completed_steps_and_rechecks_interrupted_steps():
    runtime = object.__new__(RuntimeSession)
    conversation = SimpleNamespace(
        transcript=[
            {
                "role": "plan",
                "plan": {
                    "task_id": "runtime-task",
                    "goal": "build feature",
                    "summary": "implement and verify",
                    "execution_order": ["task_1", "task_2"],
                    "steps": [
                        {
                            "id": "task_1",
                            "description": "write file",
                            "task_type": "FILE_WRITE",
                            "dependencies": [],
                            "status": "completed",
                            "result_preview": "created demo.py",
                        },
                        {
                            "id": "task_2",
                            "description": "run tests",
                            "task_type": "VERIFICATION",
                            "dependencies": ["task_1"],
                            "status": "running",
                        },
                    ],
                },
            }
        ]
    )

    restored = runtime._restore_execution_plan(conversation, "runtime-task")

    assert restored is not None
    assert restored.get_task("task_1").status.value == "COMPLETED"
    assert restored.get_task("task_1").result == "created demo.py"
    assert restored.get_task("task_2").status.value == "PENDING"
    assert "side effects are unconfirmed" in restored.get_task("task_2").description


def test_json_line_writer_preserves_chinese_text():
    stream = StringIO()
    writer = JsonLineWriter(stream)

    writer({"kind": "event", "content": "你好，StellarCode"})

    assert '"content":"你好，StellarCode"' in stream.getvalue()
    assert stream.getvalue().endswith("\n")


def test_runtime_hitl_waits_for_and_applies_desktop_decision():
    events: list[tuple[str, dict]] = []
    handler = RuntimeHitlHandler(lambda event_type, data: events.append((event_type, data)))
    result_holder = []
    request = ApprovalRequest.create(
        "execute_command",
        '{"command":"pytest -q"}',
        tool_call_id="call-pytest",
    )

    release_order: list[str] = []

    def wait_for_approval() -> None:
        result_holder.append(handler.request_approval(request))
        release_order.append("released")

    thread = threading.Thread(target=wait_for_approval)
    thread.start()
    thread.join(0.05)

    assert thread.is_alive()
    assert events[0][0] == "approval.requested"
    assert events[0][1]["tool_call_id"] == "call-pytest"
    approval_id = events[0][1]["approval_id"]
    assert (
        handler.resolve(
            approval_id,
            "approve",
            before_release=lambda: release_order.append("journaled"),
        )
        is True
    )
    thread.join(1)

    assert result_holder[0].decision == Decision.APPROVED
    assert release_order == ["journaled", "released"]
    assert handler.resolve(approval_id, "approve") is False


def test_runtime_hitl_cancelled_approval_cannot_be_reapproved():
    events: list[tuple[str, dict]] = []
    requested = threading.Event()

    def emit(event_type: str, data: dict) -> None:
        events.append((event_type, data))
        requested.set()

    handler = RuntimeHitlHandler(emit)
    results = []
    request = ApprovalRequest.create(
        "execute_command",
        '{"command":"pytest -q"}',
        tool_call_id="call-cancelled",
    )

    def wait_for_approval() -> None:
        with llm_runtime_scope("session-one", "task-one"):
            results.append(handler.request_approval(request))

    thread = threading.Thread(target=wait_for_approval)
    thread.start()
    assert requested.wait(1)
    approval_id = events[0][1]["approval_id"]

    handler.reject_task("task-one", "Task cancelled by user.")

    assert handler.resolve(approval_id, "approve") is False
    assert handler.context(approval_id) is None
    thread.join(1)
    assert not thread.is_alive()
    assert results[0].decision == Decision.REJECTED
    assert results[0].reason == "Task cancelled by user."


def test_runtime_hitl_does_not_enqueue_approval_cancelled_during_registration():
    checks = 0
    events: list[tuple[str, dict]] = []

    def is_cancelled(_task_id: str) -> bool:
        nonlocal checks
        checks += 1
        return checks >= 2

    handler = RuntimeHitlHandler(
        lambda event_type, data: events.append((event_type, data)),
        is_task_cancelled=is_cancelled,
    )
    request = ApprovalRequest.create(
        "execute_command",
        '{"command":"pytest -q"}',
        tool_call_id="call-racing-cancel",
    )

    with llm_runtime_scope("session-one", "task-one"):
        result = handler.request_approval(request)

    assert result.decision == Decision.REJECTED
    assert result.reason == "Task cancelled by user."
    assert events == []


def test_runtime_reset_persists_the_current_event_floor(tmp_path):
    journal = EventJournal(tmp_path / "events.jsonl")
    emitter = RuntimeEventEmitter(lambda _message: None, journal)
    emitter.emit("tool.started", {}, session_id="session-one", task_id="task-one")
    emitter.emit("tool.completed", {}, session_id="session-one", task_id="task-one")
    saved_floors: list[int] = []
    conversation = SimpleNamespace(
        agent=SimpleNamespace(messages=[{}, {}, {}], reset=lambda: None),
        team_agent=SimpleNamespace(reset=lambda: None),
        memory_manager=SimpleNamespace(clear_short_term=lambda: None),
        usage_ledger=None,
        transcript=[{"role": "user", "content": "hello"}],
        updated_at="",
        event_floor_sequence=0,
    )
    runtime = object.__new__(RuntimeSession)
    runtime.event_journal = journal
    runtime.settings = SimpleNamespace(context_window=200_000)
    runtime.hitl_handler = SimpleNamespace(clear_approved_all=lambda: None)
    runtime.skill_context_buffer = SimpleNamespace(clear=lambda: None)
    runtime._get_conversation = lambda _conversation_id: conversation
    runtime._save_conversation = lambda value: saved_floors.append(value.event_floor_sequence)

    cleared = runtime.reset("session-one")

    assert cleared == 2
    assert conversation.transcript == []
    assert conversation.event_floor_sequence == 2
    assert saved_floors == [2]


def test_sidecar_journals_reset_before_acknowledging_it(tmp_path):
    order: list[str] = []
    server = SidecarServer(tmp_path, lambda _message: order.append("response"))
    server.runtime = SimpleNamespace(reset=lambda _session_id: 3)
    server.active_session_id = "session-one"
    server._emit_event = lambda *_args, **_kwargs: order.append("journal")  # type: ignore[method-assign]

    server._reset_session("reset-one", {"session_id": "session-one"})

    assert order == ["journal", "response"]


def test_sidecar_rejects_approval_resolution_for_another_task(tmp_path):
    messages: list[dict] = []
    resolve_calls: list[str] = []
    server = SidecarServer(tmp_path, messages.append)
    server.runtime = SimpleNamespace(
        resolve_approval=lambda *_args, **_kwargs: resolve_calls.append("called") or True
    )
    server.active_session_id = "session-one"
    server.active_task_id = "task-one"

    server._resolve_approval(
        "approval-one",
        {
            "session_id": "session-one",
            "task_id": "task-other",
            "approval_id": "approval-id",
            "decision": "approve",
        },
    )

    assert resolve_calls == []
    assert messages[-1]["ok"] is False
    assert messages[-1]["error"]["code"] == "approval_context_mismatch"


@pytest.mark.parametrize("phase", ["cancelling", "finalizing"])
def test_sidecar_rejects_approval_resolution_for_non_running_task(tmp_path, phase):
    messages: list[dict] = []
    resolve_calls: list[str] = []
    runtime = SimpleNamespace(
        project_id="project-one",
        approval_context=lambda _approval_id: ("session-one", "task-one"),
        resolve_approval=lambda *_args, **_kwargs: resolve_calls.append("called") or True,
    )
    server = SidecarServer(tmp_path, messages.append)
    server.runtime = runtime
    server.project_id = "project-one"
    server._register_task_route(
        runtime,
        "session-one",
        "task-one",
        phase=phase,
    )

    server._resolve_approval(
        "approval-one",
        {
            "session_id": "session-one",
            "task_id": "task-one",
            "approval_id": "approval-id",
            "decision": "approve",
        },
    )

    assert resolve_calls == []
    assert messages[-1]["ok"] is False
    assert messages[-1]["error"]["code"] == "approval_not_pending"


def test_runtime_session_switches_access_mode_without_persisting_it():
    runtime = object.__new__(RuntimeSession)
    runtime.hitl_handler = RuntimeHitlHandler(lambda _event_type, _data: None)
    runtime.access_mode = "restricted"

    runtime.set_access_mode("full-access")

    assert runtime.access_mode == "full-access"
    assert runtime.hitl_handler.is_enabled() is False

    runtime.set_access_mode("restricted")
    assert runtime.hitl_handler.is_enabled() is True

    with pytest.raises(ValueError, match="unsupported access mode"):
        runtime.set_access_mode("unlimited")


def test_runtime_session_keeps_access_mode_per_idle_conversation():
    runtime = object.__new__(RuntimeSession)
    first = SimpleNamespace(access_mode="restricted")
    second = SimpleNamespace(access_mode="restricted")
    conversations = {"session-one": first, "session-two": second}
    runtime._get_conversation = conversations.__getitem__
    runtime.active_task_for_conversation = lambda session_id: (
        "task-one" if session_id == "session-one" else None
    )

    result = runtime.set_conversation_access_mode("session-two", "full-access")

    assert result == {"session_id": "session-two", "mode": "full-access"}
    assert first.access_mode == "restricted"
    assert second.access_mode == "full-access"
    with pytest.raises(RuntimeError, match="while this conversation is running"):
        runtime.set_conversation_access_mode("session-one", "full-access")


def test_sidecar_allows_access_change_for_idle_conversation_while_another_runs(tmp_path):
    messages: list[dict] = []
    changes: list[tuple[str, str]] = []
    runtime = SimpleNamespace(
        project_id="project-one",
        set_conversation_access_mode=lambda session_id, mode: (
            changes.append((session_id, mode)) or {"session_id": session_id, "mode": mode}
        ),
    )
    server = SidecarServer(tmp_path, messages.append)
    server.runtime = runtime
    server.project_id = "project-one"
    server.active_session_id = "session-idle"
    server._emit_event = lambda *_args, **_kwargs: None  # type: ignore[method-assign]
    server._register_task_route(runtime, "session-running", "task-running")

    server._set_access_mode(
        "access-idle",
        {"session_id": "session-idle", "mode": "full-access"},
    )

    assert changes == [("session-idle", "full-access")]
    assert messages[-1]["ok"] is True
    assert messages[-1]["result"] == {
        "session_id": "session-idle",
        "mode": "full-access",
    }

    with pytest.raises(RuntimeError, match="this conversation task"):
        server._set_access_mode(
            "access-running",
            {"session_id": "session-running", "mode": "full-access"},
        )


def test_runtime_session_cancels_only_the_matching_active_task():
    runtime = object.__new__(RuntimeSession)
    runtime._task_lock = threading.RLock()
    runtime._active_task_id = "task-active"
    runtime._task_cancel_event = threading.Event()
    runtime.hitl_handler = RuntimeHitlHandler(lambda _event_type, _data: None)

    assert runtime.cancel_task("task-other") is False
    assert not runtime._task_cancel_event.is_set()

    assert runtime.cancel_task("task-active") is True
    assert runtime._task_cancel_event.is_set()


def test_runtime_tracks_and_cancels_tasks_per_conversation():
    runtime = object.__new__(RuntimeSession)
    runtime._task_lock = threading.RLock()
    runtime._task_cancel_events = {}
    runtime._task_conversations = {}
    runtime._conversation_tasks = {}
    rejected: list[str] = []
    runtime.hitl_handler = SimpleNamespace(
        reject_task=lambda task_id, _reason: rejected.append(task_id),
    )

    runtime._register_task("task-one", "session-one")
    runtime._register_task("task-two", "session-two")

    assert runtime.active_task_for_conversation("session-one") == "task-one"
    assert runtime.active_task_for_conversation("session-two") == "task-two"
    assert {item["task_id"] for item in runtime.active_tasks()} == {"task-one", "task-two"}
    with pytest.raises(RuntimeError, match="already running"):
        runtime._register_task("task-three", "session-one")

    assert runtime.cancel_task("task-one") is True
    assert runtime._task_cancel_events["task-one"].is_set()
    assert not runtime._task_cancel_events["task-two"].is_set()
    assert rejected == ["task-one"]


def test_sidecar_routes_background_task_cancellation_to_its_project(tmp_path):
    messages: list[dict] = []
    cancelled: list[str] = []
    project_one = SimpleNamespace(
        project_id="project-one",
        cancel_task=lambda task_id: cancelled.append(task_id) or True,
    )
    project_two = SimpleNamespace(project_id="project-two")
    server = SidecarServer(tmp_path, messages.append)
    server.runtime = project_two
    server.project_id = "project-two"
    server._register_task_route(project_one, "session-one", "task-one")
    server._register_task_route(project_two, "session-two", "task-two")

    server._cancel_task(
        "cancel-one",
        {"session_id": "session-one", "task_id": "task-one"},
    )

    assert cancelled == ["task-one"]
    assert messages[-1]["result"] == {"accepted": True, "task_id": "task-one"}
    assert server.task_routes["task-one"].phase == "cancelling"
    assert server.session_tasks["session-two"] == "task-two"


def test_sidecar_rejects_non_object_params_inside_protocol_error_boundary(tmp_path):
    messages: list[dict] = []
    server = SidecarServer(tmp_path, messages.append)

    keep_running = server.handle(
        {
            "kind": "request",
            "protocol_version": PROTOCOL_VERSION,
            "request_id": "invalid-params",
            "method": "runtime.ping",
            "params": ["not", "an", "object"],
        }
    )

    assert keep_running is True
    assert messages[-1]["ok"] is False
    assert messages[-1]["error"] == {
        "code": "invalid_message",
        "message": "params must be an object",
    }


def test_sidecar_rejects_inconsistent_task_and_session_project_routes(tmp_path):
    server = SidecarServer(tmp_path, lambda _message: None)
    project_one = SimpleNamespace(project_id="project-one")
    project_two = SimpleNamespace(project_id="project-two")
    server.runtimes = {"project-one": project_one, "project-two": project_two}
    server._register_task_route(project_two, "session-one", "task-two")
    server.session_projects["session-one"] = "project-one"

    with pytest.raises(ValueError, match="but task task-two belongs to project project-two"):
        server._require_runtime(
            {"session_id": "session-one", "task_id": "task-two"},
            session_id="session-one",
            task_id="task-two",
        )


def test_background_rag_job_does_not_replace_current_project_alias(tmp_path, monkeypatch):
    messages: list[dict] = []
    current = SimpleNamespace(project_id="project-current")
    background = SimpleNamespace(
        project_id="project-background",
        rag_snapshot=lambda: {"sources": ["src"], "source_count": 1},
    )
    server = SidecarServer(tmp_path, messages.append)
    server.runtime = current
    server.project_id = "project-current"
    server.runtimes = {
        "project-current": current,
        "project-background": background,
    }
    server.active_rag_job_id = "rag-current"

    class DeferredThread:
        def __init__(self, **_kwargs):
            pass

        def start(self) -> None:
            pass

    monkeypatch.setattr("stellarcode.runtime.sidecar.threading.Thread", DeferredThread)

    server._start_rag_index("rag-background", {"project_id": "project-background"})

    assert messages[-1]["ok"] is True
    assert server.rag_jobs["project-background"].startswith("rag-")
    assert server.active_rag_job_id == "rag-current"


def test_legacy_approval_fallback_uses_target_project_active_context(tmp_path):
    messages: list[dict] = []
    resolved: list[tuple[str, str]] = []
    current = SimpleNamespace(project_id="project-current")
    background = SimpleNamespace(
        project_id="project-background",
        resolve_approval=lambda approval_id, decision, *_args, **_kwargs: (
            resolved.append((approval_id, decision)) or True
        ),
    )
    server = SidecarServer(tmp_path, messages.append)
    server.runtime = current
    server.project_id = "project-current"
    server.runtimes = {
        "project-current": current,
        "project-background": background,
    }
    server._register_task_route(
        background,
        "session-background",
        "task-background",
        phase="running",
    )
    server.project_active_sessions["project-background"] = "session-background"
    server.active_session_id = "session-current"
    server.active_task_id = "task-current"
    server._emit_event = lambda *_args, **_kwargs: None  # type: ignore[method-assign]

    server._resolve_approval(
        "approval-response",
        {
            "project_id": "project-background",
            "approval_id": "approval-background",
            "decision": "approve",
        },
    )

    assert resolved == [("approval-background", "approve")]
    assert messages[-1]["ok"] is True


def test_workspace_close_clears_project_active_session_alias(tmp_path):
    messages: list[dict] = []
    closed: list[str] = []
    runtime = SimpleNamespace(
        project_id="project-one",
        close=lambda: closed.append("project-one"),
    )
    server = SidecarServer(tmp_path, messages.append)
    server.runtime = runtime
    server.project_id = "project-one"
    server.runtimes = {"project-one": runtime}
    server.project_active_sessions["project-one"] = "session-one"

    server._close_workspace("close-one", {"project_id": "project-one"})

    assert closed == ["project-one"]
    assert "project-one" not in server.project_active_sessions
    assert server.runtime is None
    assert any(message.get("request_id") == "close-one" and message.get("ok") for message in messages)


@pytest.mark.parametrize("phase", ["cancelling", "finalizing"])
def test_sidecar_does_not_cancel_after_cancellation_or_finalization_started(
    tmp_path,
    phase,
):
    messages: list[dict] = []
    cancellation_calls: list[str] = []
    runtime = SimpleNamespace(
        project_id="project-one",
        cancel_task=lambda task_id: cancellation_calls.append(task_id) or True,
    )
    server = SidecarServer(tmp_path, messages.append)
    server.runtime = runtime
    server.project_id = "project-one"
    server._register_task_route(
        runtime,
        "session-one",
        "task-one",
        phase=phase,
    )

    server._cancel_task(
        "cancel-one",
        {"session_id": "session-one", "task_id": "task-one"},
    )

    assert cancellation_calls == []
    assert messages[-1]["ok"] is True
    assert messages[-1]["result"] == {"accepted": False, "task_id": "task-one"}
    assert server.task_routes["task-one"].phase == phase


def test_sidecar_returns_not_accepted_after_terminal_route_was_released(tmp_path):
    messages: list[dict] = []
    cancellation_calls: list[str] = []
    runtime = SimpleNamespace(
        project_id="project-one",
        cancel_task=lambda task_id: cancellation_calls.append(task_id) or True,
    )
    server = SidecarServer(tmp_path, messages.append)
    server.runtime = runtime
    server.project_id = "project-one"
    server._register_task_route(runtime, "session-one", "task-one", phase="running")
    server._release_task_route("task-one")

    server._cancel_task(
        "cancel-terminal",
        {"session_id": "session-one", "task_id": "task-one"},
    )

    assert cancellation_calls == []
    assert messages[-1]["ok"] is True
    assert messages[-1]["result"] == {"accepted": False, "task_id": "task-one"}


def test_sidecar_cancel_and_finalization_phase_transition_is_atomic(tmp_path):
    messages: list[dict] = []
    cancel_entered = threading.Event()
    release_cancel = threading.Event()
    finalization_entered = threading.Event()
    transition_errors: list[Exception] = []

    def cancel_task(_task_id: str) -> bool:
        cancel_entered.set()
        assert release_cancel.wait(1)
        return True

    runtime = SimpleNamespace(project_id="project-one", cancel_task=cancel_task)
    server = SidecarServer(tmp_path, messages.append)
    server.runtime = runtime
    server.project_id = "project-one"
    server._register_task_route(runtime, "session-one", "task-one", phase="running")

    cancel_thread = threading.Thread(
        target=server._cancel_task,
        args=(
            "cancel-one",
            {"session_id": "session-one", "task_id": "task-one"},
        ),
    )

    def start_finalization() -> None:
        finalization_entered.set()
        try:
            server.task_state.transition("task-one", "finalizing")
        except Exception as exc:  # pragma: no cover - assertion reports the concrete race
            transition_errors.append(exc)

    cancel_thread.start()
    assert cancel_entered.wait(1)
    finalization_thread = threading.Thread(target=start_finalization)
    finalization_thread.start()
    assert finalization_entered.wait(1)
    # Finalization must wait until cancellation has atomically committed the
    # cancelling phase; it can then advance through the allowed transition.
    assert finalization_thread.is_alive()
    release_cancel.set()
    cancel_thread.join(1)
    finalization_thread.join(1)

    assert not cancel_thread.is_alive()
    assert not finalization_thread.is_alive()
    assert transition_errors == []
    assert messages[-1]["ok"] is True
    assert messages[-1]["result"] == {"accepted": True, "task_id": "task-one"}
    assert server.task_routes["task-one"].phase == "finalizing"


def test_answer_ready_recovery_does_not_call_the_model_and_releases_runtime_task(tmp_path):
    runtime = object.__new__(RuntimeSession)
    runtime._task_lock = threading.RLock()
    runtime._active_task_id = "task-ready"
    runtime._active_conversation_id = "session-ready"
    runtime._task_cancel_event = threading.Event()
    runtime.task_checkpoints = TaskCheckpointStore(tmp_path / "active-task.json")
    runtime.task_checkpoints.write(
        {
            "task_id": "task-ready",
            "session_id": "session-ready",
            "status": "answer_ready",
            "answer": "already finished",
        }
    )
    conversation = SimpleNamespace(
        transcript=[{"role": "assistant", "content": "already finished"}],
    )
    runtime.conversations = {"session-ready": conversation}

    answer = runtime.resume_task("task-ready", "session-ready")

    assert answer == "already finished"
    assert runtime._active_task_id is None
    assert runtime._task_cancel_event is None


def test_runtime_trace_setting_is_scoped_to_the_active_conversation(tmp_path):
    runtime = object.__new__(RuntimeSession)
    runtime.workspace = tmp_path
    runtime.project_id = "project-test"
    runtime.trace_recorder = TraceRecorder(tmp_path / ".stellarcode" / "traces")
    runtime._trace_conversation_id = None
    runtime._active_conversation_id = "session-one"
    conversation = SimpleNamespace(
        id="session-one",
        title="Trace test",
        trace_enabled=False,
        trace_path=None,
        updated_at="",
    )
    runtime.conversations = {conversation.id: conversation}
    runtime._save_conversation = lambda _conversation: None

    enabled = runtime.set_trace("session-one", True)

    assert enabled["enabled"] is True
    assert runtime.trace_recorder.enabled
    assert runtime._trace_conversation_id == "session-one"
    assert enabled["path"]
    assert list((tmp_path / ".stellarcode" / "traces").glob("*.jsonl"))

    disabled = runtime.set_trace("session-one", False)
    assert disabled["enabled"] is False
    assert not runtime.trace_recorder.enabled


def test_sidecar_exposes_conversation_trace_toggle(tmp_path):
    messages: list[dict] = []
    recorded_events: list[tuple[str, dict]] = []
    fake_runtime = SimpleNamespace(
        trace_recorder=SimpleNamespace(record=lambda *_args, **_kwargs: None),
        set_trace=lambda session_id, enabled: {
            "enabled": enabled,
            "path": str(tmp_path / f"{session_id}.jsonl"),
        },
        record_runtime_event=lambda event_type, data, _session_id, _task_id: recorded_events.append(
            (event_type, data)
        ),
    )
    server = SidecarServer(tmp_path, messages.append)
    server.runtime = fake_runtime
    server.active_session_id = "session-one"

    assert server.handle(
        {
            "kind": "request",
            "protocol_version": PROTOCOL_VERSION,
            "request_id": "request-trace",
            "method": "session.set_trace",
            "params": {"session_id": "session-one", "enabled": True},
        }
    )

    assert messages[0]["ok"] is True
    assert messages[0]["result"]["enabled"] is True
    assert messages[1]["type"] == "trace.status_changed"
    assert recorded_events[0][0] == "trace.status_changed"


def test_sidecar_mcp_install_requires_explicit_stdio_confirmation(tmp_path):
    messages: list[dict] = []
    installs: list[tuple[str, object, bool]] = []
    fake_runtime = SimpleNamespace(
        trace_recorder=SimpleNamespace(record=lambda *_args, **_kwargs: None),
        install_mcp_server=lambda name, config, overwrite=False: (
            installs.append((name, config, overwrite))
            or {"servers": [], "ready_servers": 0, "total_servers": 0, "total_tools": 0}
        ),
    )
    server = SidecarServer(tmp_path, messages.append)
    server.runtime = fake_runtime
    request = {
        "kind": "request",
        "protocol_version": PROTOCOL_VERSION,
        "request_id": "request-mcp-install",
        "method": "mcp.install",
        "params": {
            "name": "local-server",
            "config": {"command": "python", "args": ["server.py"]},
        },
    }

    assert server.handle(request)
    assert messages[-1]["ok"] is False
    assert "confirmed=true" in messages[-1]["error"]["message"]
    assert installs == []

    request["request_id"] = "request-mcp-install-confirmed"
    request["params"]["confirmed"] = True
    assert server.handle(request)

    assert messages[-1]["ok"] is True
    assert installs[0][0] == "local-server"
    assert installs[0][1].command == "python"
    assert installs[0][1].args == ["server.py"]


def test_sidecar_routes_mcp_management_requests(tmp_path):
    messages: list[dict] = []
    calls: list[tuple] = []
    snapshot = {
        "servers": [],
        "ready_servers": 0,
        "total_servers": 0,
        "total_tools": 0,
    }
    fake_runtime = SimpleNamespace(
        trace_recorder=SimpleNamespace(record=lambda *_args, **_kwargs: None),
        mcp_snapshot=lambda: snapshot,
        set_mcp_server_enabled=lambda name, enabled: (
            calls.append(("enabled", name, enabled)) or snapshot
        ),
        restart_mcp_server=lambda name: calls.append(("restart", name)) or snapshot,
        remove_mcp_server=lambda name: calls.append(("remove", name)) or snapshot,
        mcp_server_logs=lambda name: {"name": name, "logs": "stderr line"},
    )
    server = SidecarServer(tmp_path, messages.append)
    server.runtime = fake_runtime

    requests = [
        ("mcp.list", {}),
        ("mcp.set_enabled", {"name": "demo", "enabled": False}),
        ("mcp.restart", {"name": "demo"}),
        ("mcp.remove", {"name": "demo"}),
        ("mcp.logs", {"name": "demo"}),
    ]
    for index, (method, params) in enumerate(requests):
        assert server.handle(
            {
                "kind": "request",
                "protocol_version": PROTOCOL_VERSION,
                "request_id": f"request-mcp-{index}",
                "method": method,
                "params": params,
            }
        )

    assert all(message["ok"] is True for message in messages)
    assert calls == [
        ("enabled", "demo", False),
        ("restart", "demo"),
        ("remove", "demo"),
    ]
    assert messages[-1]["result"] == {"name": "demo", "logs": "stderr line"}


def test_sidecar_routes_rag_sources_and_streams_background_index_events(tmp_path):
    source = tmp_path / "source.py"
    source.write_text("def demo(): return True", encoding="utf-8")
    messages: list[dict] = []
    snapshot = {
        "workspace": str(tmp_path),
        "sources": [{"path": str(source), "kind": "file", "added_at": "now"}],
        "source_count": 1,
        "indexed_file_count": 0,
        "chunk_count": 0,
        "relation_count": 0,
    }

    def rebuild(progress_callback):
        progress_callback("Discovered 1 file(s) to index")
        return {**snapshot, "indexed_file_count": 1, "chunk_count": 2}

    fake_runtime = SimpleNamespace(
        trace_recorder=SimpleNamespace(record=lambda *_args, **_kwargs: None),
        rag_snapshot=lambda: snapshot,
        add_rag_sources=lambda paths: snapshot,
        rebuild_rag_index=rebuild,
        record_runtime_event=lambda *_args, **_kwargs: None,
    )
    server = SidecarServer(tmp_path, messages.append)
    server.runtime = fake_runtime
    server.project_id = "project-rag"

    assert server.handle(
        {
            "kind": "request",
            "protocol_version": PROTOCOL_VERSION,
            "request_id": "request-rag-add",
            "method": "rag.add_sources",
            "params": {"paths": [str(source)]},
        }
    )
    assert messages[-1]["ok"] is True

    assert server.handle(
        {
            "kind": "request",
            "protocol_version": PROTOCOL_VERSION,
            "request_id": "request-rag-index",
            "method": "rag.index",
            "params": {},
        }
    )
    deadline = time.monotonic() + 1
    while server.active_rag_job_id and time.monotonic() < deadline:
        time.sleep(0.01)

    event_types = [message.get("type") for message in messages if message.get("kind") == "event"]
    assert messages[1]["result"]["status"] == "indexing"
    assert event_types == ["rag.index.started", "rag.index.progress", "rag.index.completed"]
    completed = next(
        message for message in messages if message.get("type") == "rag.index.completed"
    )
    assert completed["data"]["snapshot"]["chunk_count"] == 2


def test_sidecar_recovers_an_unfinished_task_with_the_same_task_id(tmp_path):
    messages: list[dict] = []
    completed_checkpoints: list[str] = []
    checkpoint = {
        "task_id": "task-recover",
        "session_id": "session-one",
        "prompt": "finish the task",
        "started_at": "2026-08-10T00:00:00.000Z",
        "recovery_attempts": 1,
    }
    fake_runtime = SimpleNamespace(
        trace_recorder=SimpleNamespace(record=lambda *_args, **_kwargs: None),
        open_conversation=lambda session_id: {"id": session_id},
        prepare_recovery=lambda task_id, session_id: dict(checkpoint),
        pending_recovery=lambda: {
            **checkpoint,
            "mode": "react",
            "status": "recovering",
            "checkpoint_stage": "runtime_restarted",
            "prompt_preview": "finish the task",
        },
        get_mode=lambda _session_id: "react",
        resume_task=lambda _task_id, _session_id: "recovered answer",
        event_journal=SimpleNamespace(task_is_terminal=lambda _task_id: True),
        mark_task_finalize_pending=lambda *_args: None,
        finalize_pending_task=lambda _task_id, _session_id: (
            "completed",
            {"status": "completed"},
        ),
        complete_task_checkpoint=completed_checkpoints.append,
        record_runtime_event=lambda *_args, **_kwargs: None,
    )
    server = SidecarServer(tmp_path, messages.append)
    server.runtime = fake_runtime

    assert server.handle(
        {
            "kind": "request",
            "protocol_version": PROTOCOL_VERSION,
            "request_id": "request-recover",
            "method": "task.recover",
            "params": {"session_id": "session-one", "task_id": "task-recover"},
        }
    )
    deadline = time.monotonic() + 1
    while server.active_task_id and time.monotonic() < deadline:
        time.sleep(0.01)

    assert messages[0]["ok"] is True
    assert messages[0]["result"]["task_id"] == "task-recover"
    assert any(
        message.get("type") == "task.started"
        and message.get("task_id") == "task-recover"
        and message["data"]["recovered"] is True
        for message in messages
    )
    assert any(
        message.get("type") == "assistant.completed"
        and message["data"]["content"] == "recovered answer"
        for message in messages
    )
    assert completed_checkpoints == ["task-recover"]


def test_runtime_refuses_recovery_when_the_task_baseline_is_missing(tmp_path):
    runtime = object.__new__(RuntimeSession)
    runtime.task_checkpoints = TaskCheckpointStore(tmp_path / "active-task.json")
    runtime.workspace_protection = SimpleNamespace(
        validate_recovery_baseline=lambda *_args: (_ for _ in ()).throw(
            RuntimeError("baseline record missing")
        )
    )
    runtime._task_lock = threading.RLock()
    runtime._active_task_id = None
    runtime._active_conversation_id = None
    runtime._task_cancel_event = None
    runtime.task_checkpoints.write(
        {
            "task_id": "task-recover",
            "session_id": "session-one",
            "status": "prepared",
            "workspace_snapshot": {"snapshot_id": "snapshot-one"},
        }
    )

    with pytest.raises(RuntimeError, match="cannot resume safely"):
        runtime.prepare_recovery("task-recover", "session-one")

    assert runtime._active_task_id is None
