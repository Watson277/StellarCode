from __future__ import annotations

import threading
import time
from typing import Any

import pytest

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
    agent = PlanExecuteAgent(client, registry)

    result = agent.run("write and verify a file")

    assert "Plan status: COMPLETED" in result
    assert "task_1 [COMPLETED]" in result
    assert "task_2 [COMPLETED]" in result


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


def test_should_plan_for_complex_chinese_prompt():
    assert should_plan("创建一个文件，然后写入内容，并且测试验证")
    assert not should_plan("你好")
