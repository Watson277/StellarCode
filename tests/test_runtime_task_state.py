from __future__ import annotations

import pytest

from stellarcode.runtime.task_state import RuntimeTaskStateMachine


def test_runtime_task_state_machine_tracks_independent_conversations() -> None:
    machine = RuntimeTaskStateMachine()
    runtime_one = object()
    runtime_two = object()

    machine.register(runtime_one, "project-one", "session-one", "task-one")
    machine.register(runtime_one, "project-one", "session-two", "task-two")
    machine.register(runtime_two, "project-two", "session-three", "task-three")
    machine.transition("task-one", "running")
    machine.transition("task-two", "running")
    machine.transition("task-two", "cancelling")
    machine.transition("task-two", "finalizing")

    assert machine.task_for_session("session-one") == "task-one"
    assert machine.route("task-two").phase == "finalizing"  # type: ignore[union-attr]
    assert machine.has_project_tasks("project-one") is True
    assert machine.has_project_tasks("project-two") is True
    assert {task_id for task_id, _route in machine.routes_for_project("project-one")} == {
        "task-one",
        "task-two",
    }

    machine.release("task-one")
    assert machine.task_for_session("session-one") is None
    assert machine.active_task_ids() == {"task-two", "task-three"}


def test_runtime_task_state_machine_rejects_invalid_ownership_and_transitions() -> None:
    machine = RuntimeTaskStateMachine()
    runtime = object()
    machine.register(runtime, "project-one", "session-one", "task-one")

    with pytest.raises(RuntimeError, match="already has a running task"):
        machine.register(runtime, "project-one", "session-one", "task-two")
    with pytest.raises(RuntimeError, match="another conversation"):
        machine.register(runtime, "project-one", "session-two", "task-one")

    machine.transition("task-one", "running")
    machine.transition("task-one", "finalizing")
    with pytest.raises(RuntimeError, match="invalid task route transition"):
        machine.transition("task-one", "running")


def test_cancel_can_arrive_before_the_worker_starts() -> None:
    machine = RuntimeTaskStateMachine()
    machine.register(object(), "project-one", "session-one", "task-one")

    cancelled = machine.transition("task-one", "cancelling")

    assert cancelled.phase == "cancelling"
    assert machine.transition("task-one", "finalizing").phase == "finalizing"
