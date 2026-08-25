from __future__ import annotations

import threading
import time
from typing import Any

import pytest

from stellarcode.memory import MemoryManager
from stellarcode.plan import (
    ExecutionPlan,
    PlanExecuteAgent,
    PlanValidationError,
    Planner,
    Task,
    TaskStatus,
    TaskType,
    should_plan,
)
from stellarcode.tools import build_default_registry


class FakePlanClient:
    def __init__(self) -> None:
        self.calls: list[list[dict[str, Any]]] = []

    def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        temperature: float = 0.2,
    ) -> dict[str, Any]:
        self.calls.append(messages)
        if "planner" in messages[0]["content"]:
            return {
                "role": "assistant",
                "content": """
```json
{
  "summary": "write and verify",
  "tasks": [
    {
      "id": "make_file",
      "description": "write hello.txt",
      "type": "FILE_WRITE",
      "dependencies": []
    },
    {
      "id": "verify_file",
      "description": "read hello.txt and verify it",
      "type": "VERIFICATION",
      "dependencies": ["make_file"]
    }
  ]
}
```
""",
            }
        return {"role": "assistant", "content": "task done"}


def test_planner_parses_json_and_normalizes_ids():
    planner = Planner(FakePlanClient())

    plan = planner.create_plan("write and verify a file")

    assert plan.summary == "write and verify"
    assert list(plan.tasks) == ["task_1", "task_2"]
    assert plan.tasks["task_2"].dependencies == ["task_1"]
    assert plan.execution_order == ["task_1", "task_2"]


def test_planner_accepts_multi_agent_steps_field():
    planner = Planner(FakePlanClient())

    plan = planner.parse_plan(
        "inspect then verify",
        """
        {
          "summary": "team plan",
          "steps": [
            {"id": "inspect", "description": "inspect", "type": "ANALYSIS", "dependencies": []},
            {
              "id": "verify",
              "description": "verify",
              "type": "VERIFICATION",
              "dependencies": ["inspect"]
            }
          ]
        }
        """,
    )

    assert list(plan.tasks) == ["task_1", "task_2"]
    assert plan.tasks["task_2"].dependencies == ["task_1"]


def test_execution_plan_detects_cycles():
    plan = ExecutionPlan(id="plan_1", goal="cycle")
    plan.add_task(Task(id="task_1", description="a", type=TaskType.ANALYSIS))
    plan.add_task(Task(id="task_2", description="b", type=TaskType.ANALYSIS))
    plan.tasks["task_1"].dependencies = ["task_2"]
    plan.tasks["task_2"].dependencies = ["task_1"]

    with pytest.raises(PlanValidationError, match="cycle"):
        plan.compute_execution_order()


def test_execution_plan_groups_independent_tasks_into_dag_batches():
    plan = ExecutionPlan(id="plan_1", goal="batches")
    plan.add_task(Task("task_1", "first root", TaskType.ANALYSIS))
    plan.add_task(Task("task_2", "second root", TaskType.ANALYSIS))
    plan.add_task(
        Task(
            "task_3",
            "dependent",
            TaskType.ANALYSIS,
            dependencies=["task_1", "task_2"],
        )
    )

    batches = plan.execution_batches()

    assert [[task.id for task in batch] for batch in batches] == [
        ["task_1", "task_2"],
        ["task_3"],
    ]


def test_execution_plan_uses_java_compatible_dfs_topological_order():
    plan = ExecutionPlan(id="plan_1", goal="topological order")
    plan.add_task(Task("task_1", "first root", TaskType.ANALYSIS))
    plan.add_task(Task("task_2", "second root", TaskType.ANALYSIS))
    plan.add_task(
        Task("task_3", "depends on second", TaskType.ANALYSIS, dependencies=["task_2"])
    )
    plan.add_task(
        Task("task_4", "depends on first", TaskType.ANALYSIS, dependencies=["task_1"])
    )

    assert plan.topological_sort() == ["task_1", "task_2", "task_3", "task_4"]
    assert plan.compute_execution_order() == ["task_1", "task_2", "task_3", "task_4"]
    assert [[task.id for task in batch] for batch in plan.execution_batches()] == [
        ["task_1", "task_2"],
        ["task_4", "task_3"],
    ]


def test_execution_plan_deduplicates_repeated_dependency_edges():
    plan = ExecutionPlan(id="plan_1", goal="duplicate edge")
    plan.add_task(Task("task_1", "root", TaskType.ANALYSIS))
    plan.add_task(
        Task(
            "task_2",
            "dependent",
            TaskType.ANALYSIS,
            dependencies=["task_1", "task_1"],
        )
    )

    assert plan.compute_execution_order() == ["task_1", "task_2"]


def test_task_executable_requires_completed_dependencies():
    plan = ExecutionPlan(id="plan_1", goal="deps")
    plan.add_task(Task(id="task_1", description="first", type=TaskType.ANALYSIS))
    plan.add_task(
        Task(
            id="task_2",
            description="second",
            type=TaskType.ANALYSIS,
            dependencies=["task_1"],
        )
    )

    assert not plan.tasks["task_2"].is_executable(plan.tasks)
    plan.tasks["task_1"].mark_completed("done")
    assert plan.tasks["task_2"].is_executable(plan.tasks)


def test_plan_execute_agent_runs_tasks_in_order(tmp_path):
    registry = build_default_registry(tmp_path)
    client = FakePlanClient()
    events: list[tuple[str, dict[str, object]]] = []
    agent = PlanExecuteAgent(
        client,
        registry,
        event_callback=lambda event_type, data: events.append((event_type, data)),
    )

    result = agent.run("write and verify a file")

    assert "Plan status: COMPLETED" in result
    assert "task_1 [COMPLETED]" in result
    assert "task_2 [COMPLETED]" in result
    event_types = [event_type for event_type, _data in events]
    assert event_types == [
        "plan.planning.started",
        "plan.created",
        "plan.step.started",
        "plan.step.completed",
        "plan.step.started",
        "plan.step.completed",
    ]
    created = next(data for event_type, data in events if event_type == "plan.created")
    assert created["summary"] == "write and verify"
    assert created["execution_order"] == ["task_1", "task_2"]
    assert created["tasks"] == [
        {
            "id": "task_1",
            "description": "write hello.txt",
            "task_type": "FILE_WRITE",
            "dependencies": [],
        },
        {
            "id": "task_2",
            "description": "read hello.txt and verify it",
            "task_type": "VERIFICATION",
            "dependencies": ["task_1"],
        },
    ]


def test_plan_step_receives_memory_as_user_context_not_system(tmp_path):
    registry = build_default_registry(tmp_path)
    manager = MemoryManager(storage_dir=tmp_path / "memory")
    manager.save_fact("Python runtime uses version 3.12")
    client = FakePlanClient()
    plan = ExecutionPlan(id="plan_memory", goal="inspect Python runtime")
    plan.add_task(Task("task_1", "inspect Python runtime", TaskType.ANALYSIS))

    PlanExecuteAgent(
        client,
        registry,
        memory_manager=manager,
    ).execute_plan(plan)

    messages = next(
        call
        for call in client.calls
        if any('"kind":"retrieved_memory"' in str(item.get("content")) for item in call)
    )
    assert "version 3.12" not in messages[0]["content"]
    assert '"kind":"retrieved_memory"' in messages[-2]["content"]
    assert messages[-1]["role"] == "user"
    assert all("_stellarcode_context" not in message for message in messages)


def test_plan_execute_agent_runs_independent_dag_tasks_in_parallel(tmp_path):
    barrier = threading.Barrier(2)
    roots_finished = threading.Event()
    finished_roots = 0
    lock = threading.Lock()

    class ParallelPlanAgent(PlanExecuteAgent):
        def _execute_task(self, plan, task):
            nonlocal finished_roots
            if task.id in {"task_1", "task_2"}:
                barrier.wait(timeout=2)
                time.sleep(0.02)
                with lock:
                    finished_roots += 1
                    if finished_roots == 2:
                        roots_finished.set()
                return task.id
            assert roots_finished.is_set()
            return "dependent complete"

    plan = ExecutionPlan(id="plan_1", goal="parallel roots")
    plan.add_task(Task("task_1", "first root", TaskType.ANALYSIS))
    plan.add_task(Task("task_2", "second root", TaskType.ANALYSIS))
    plan.add_task(
        Task(
            "task_3",
            "dependent",
            TaskType.ANALYSIS,
            dependencies=["task_1", "task_2"],
        )
    )
    agent = ParallelPlanAgent(FakePlanClient(), build_default_registry(tmp_path))

    result = agent.execute_plan(plan)

    assert "Plan status: COMPLETED" in result
    assert plan.tasks["task_3"].result == "dependent complete"


def test_skip_blocked_tasks_after_failure():
    plan = ExecutionPlan(id="plan_1", goal="failure")
    plan.add_task(Task(id="task_1", description="first", type=TaskType.ANALYSIS))
    plan.add_task(
        Task(
            id="task_2",
            description="second",
            type=TaskType.ANALYSIS,
            dependencies=["task_1"],
        )
    )
    plan.compute_execution_order()
    plan.tasks["task_1"].mark_failed("boom")

    plan.skip_blocked_tasks("task_1")

    assert plan.tasks["task_2"].status == TaskStatus.SKIPPED


def test_plan_events_report_failed_and_skipped_steps(tmp_path):
    class FailingPlanAgent(PlanExecuteAgent):
        def _execute_task(self, plan, task, cancellation_event=None):
            raise RuntimeError("boom")

    plan = ExecutionPlan(id="plan_1", goal="failure events")
    plan.add_task(Task(id="task_1", description="first", type=TaskType.ANALYSIS))
    plan.add_task(
        Task(
            id="task_2",
            description="blocked",
            type=TaskType.VERIFICATION,
            dependencies=["task_1"],
        )
    )
    events: list[tuple[str, dict[str, object]]] = []
    agent = FailingPlanAgent(
        FakePlanClient(),
        build_default_registry(tmp_path),
        event_callback=lambda event_type, data: events.append((event_type, data)),
    )

    result = agent.execute_plan(plan)

    assert "Plan status: FAILED" in result
    assert [(event_type, data.get("step_id")) for event_type, data in events] == [
        ("plan.created", None),
        ("plan.step.started", "task_1"),
        ("plan.step.failed", "task_1"),
        ("plan.step.skipped", "task_2"),
    ]


def test_should_plan_for_complex_chinese_prompt():
    assert should_plan("创建一个文件，然后写入内容，并且测试验证")
    assert not should_plan("你好")


def test_plan_step_routes_streamed_text_to_its_step_event(tmp_path):
    class StreamingStepClient:
        def chat(self, _messages, tools=None, temperature=0.2, on_delta=None):
            assert tools is not None
            assert on_delta is not None
            on_delta("partial ")
            on_delta("result")
            return {"role": "assistant", "content": "partial result"}

    plan = ExecutionPlan(id="plan_stream", goal="stream one step")
    plan.add_task(Task("task_1", "produce a result", TaskType.ANALYSIS))
    events: list[tuple[str, dict[str, object]]] = []
    agent = PlanExecuteAgent(
        StreamingStepClient(),
        build_default_registry(tmp_path),
        event_callback=lambda event_type, data: events.append((event_type, data)),
    )

    agent.execute_plan(plan)

    deltas = [data for event_type, data in events if event_type == "plan.step.delta"]
    assert "".join(str(data["text"]) for data in deltas) == "partial result"
    assert all(data["step_id"] == "task_1" for data in deltas)
