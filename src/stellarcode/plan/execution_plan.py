from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum

from stellarcode.plan.task import Task, TaskStatus


class PlanValidationError(ValueError):
    pass


class PlanStatus(str, Enum):
    CREATED = "CREATED"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


@dataclass
class ExecutionPlan:
    id: str
    goal: str
    summary: str = ""
    tasks: dict[str, Task] = field(default_factory=dict)
    execution_order: list[str] = field(default_factory=list)
    status: PlanStatus = PlanStatus.CREATED
    start_time: float | None = None
    end_time: float | None = None

    def add_task(self, task: Task) -> None:
        if task.id in self.tasks:
            raise PlanValidationError(f"Duplicate task id: {task.id}")
        self.tasks[task.id] = task

    def get_task(self, task_id: str) -> Task:
        try:
            return self.tasks[task_id]
        except KeyError as exc:
            raise PlanValidationError(f"Unknown task id: {task_id}") from exc

    def compute_execution_order(self) -> list[str]:
        batches = self.execution_batches()
        self.execution_order = [task.id for batch in batches for task in batch]
        return self.execution_order

    def execution_batches(self) -> list[list[Task]]:
        self._validate_dependencies_exist()
        remaining = dict(self.tasks)
        completed: set[str] = set()
        batches: list[list[Task]] = []

        while remaining:
            batch = [
                task
                for task in remaining.values()
                if all(dependency in completed for dependency in task.dependencies)
            ]
            if not batch:
                cycle_at = next(iter(remaining))
                raise PlanValidationError(f"Dependency cycle detected at task: {cycle_at}")
            batches.append(batch)
            for task in batch:
                remaining.pop(task.id)
                completed.add(task.id)

        return batches

    def executable_tasks(self) -> list[Task]:
        return [task for task in self.tasks.values() if task.is_executable(self.tasks)]

    def mark_started(self) -> None:
        self.status = PlanStatus.RUNNING
        self.start_time = time.time()

    def mark_completed(self) -> None:
        self.status = PlanStatus.COMPLETED
        self.end_time = time.time()

    def mark_failed(self) -> None:
        self.status = PlanStatus.FAILED
        self.end_time = time.time()

    def has_failed(self) -> bool:
        return any(task.status == TaskStatus.FAILED for task in self.tasks.values())

    def skip_blocked_tasks(self, failed_task_id: str) -> None:
        blocked = set(self._dependent_tasks(failed_task_id))
        for task_id in blocked:
            task = self.tasks[task_id]
            if task.status == TaskStatus.PENDING:
                task.mark_skipped(f"Skipped because dependency failed: {failed_task_id}")

    def visualize(self) -> str:
        if not self.execution_order:
            self.compute_execution_order()

        lines = [
            f"Plan: {self.goal}",
            f"Summary: {self.summary or '(none)'}",
            "",
            "Tasks:",
        ]
        for index, task_id in enumerate(self.execution_order, start=1):
            task = self.tasks[task_id]
            deps = ", ".join(task.dependencies) if task.dependencies else "-"
            lines.append(
                f"{index}. [{task.status.value}] {task.id} {task.type.value}: "
                f"{task.description} (deps: {deps})"
            )
        return "\n".join(lines)

    def build_result(self) -> str:
        lines = [
            f"Plan status: {self.status.value}",
            f"Goal: {self.goal}",
            "",
            "Task results:",
        ]
        for task_id in self.execution_order:
            task = self.tasks[task_id]
            lines.append(f"- {task.id} [{task.status.value}] {task.description}")
            if task.result:
                lines.append(f"  result: {task.result}")
            if task.error:
                lines.append(f"  error: {task.error}")
        return "\n".join(lines)

    def _validate_dependencies_exist(self) -> None:
        for task in self.tasks.values():
            for dependency_id in task.dependencies:
                if dependency_id not in self.tasks:
                    raise PlanValidationError(
                        f"Task {task.id} depends on unknown task: {dependency_id}"
                    )

    def _dependent_tasks(self, failed_task_id: str) -> list[str]:
        dependents: list[str] = []
        seen: set[str] = set()

        def collect(task_id: str) -> None:
            for candidate in self.tasks.values():
                if task_id in candidate.dependencies and candidate.id not in seen:
                    seen.add(candidate.id)
                    dependents.append(candidate.id)
                    collect(candidate.id)

        collect(failed_task_id)
        return dependents
