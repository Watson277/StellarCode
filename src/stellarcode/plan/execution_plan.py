"""Validated DAG model used by planning, dependency scheduling, and recovery."""

from __future__ import annotations

import time
from collections import deque
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
        """Return a deterministic linear topological order for this plan's DAG.

        The order is primarily for prompt context, UI display, and result output.
        Actual execution still uses :meth:`execution_batches` so independent tasks
        can run in parallel.
        """

        self.execution_order = self.topological_sort()
        return list(self.execution_order)

    def topological_sort(self) -> list[str]:
        """Linearise the dependency DAG with Java-compatible DFS post-order.

        ``visiting`` is the recursion stack (the Java ``visiting`` set), while
        ``visited`` records nodes whose dependencies were already emitted. This
        yields the same deterministic dependency-first order as PaiCLI's original
        ``ExecutionPlan.topologicalSort`` implementation.
        """

        self._validate_dependencies_exist()
        visited: set[str] = set()
        visiting: set[str] = set()
        order: list[str] = []

        def visit(task_id: str) -> None:
            if task_id in visiting:
                raise PlanValidationError(f"Dependency cycle detected at task: {task_id}")
            if task_id in visited:
                return

            visiting.add(task_id)
            for dependency_id in self.tasks[task_id].dependencies:
                visit(dependency_id)
            visiting.remove(task_id)
            visited.add(task_id)
            # Post-order append guarantees every dependency appears before its
            # dependent, exactly matching the Java reference implementation.
            order.append(task_id)

        for task_id in self.tasks:
            visit(task_id)
        return order

    def execution_batches(self) -> list[list[Task]]:
        """Return parallel-safe topological layers without changing DFS order."""

        # Keep the historical side effect of this public method: callers that
        # request batches can still read ``execution_order`` afterwards. The
        # linear view itself remains DFS-compatible, not a flattened batch order.
        self.execution_order = self.topological_sort()
        batches = self._topological_batches()
        return [[self.tasks[task_id] for task_id in batch] for batch in batches]

    def _topological_batches(self) -> list[list[str]]:
        self._validate_dependencies_exist()
        # ``dependencies`` is user/model supplied JSON. Treat duplicate ids as one
        # edge, otherwise a duplicate dependency would keep an artificial indegree.
        normalized_dependencies = {
            task_id: tuple(dict.fromkeys(task.dependencies))
            for task_id, task in self.tasks.items()
        }
        indegree = {
            task_id: len(dependencies)
            for task_id, dependencies in normalized_dependencies.items()
        }
        dependents: dict[str, list[str]] = {task_id: [] for task_id in self.tasks}
        for task_id, dependencies in normalized_dependencies.items():
            for dependency_id in dependencies:
                dependents[dependency_id].append(task_id)

        # Snapshot each ready frontier before reducing its outgoing edges. This
        # preserves the parallel execution boundary while also producing a valid
        # linear topological order when the frontiers are flattened.
        ready = deque(task_id for task_id, degree in indegree.items() if degree == 0)
        batches: list[list[str]] = []
        visited = 0
        while ready:
            current_batch = list(ready)
            ready.clear()
            batches.append(current_batch)
            visited += len(current_batch)
            for task_id in current_batch:
                for dependent_id in dependents[task_id]:
                    indegree[dependent_id] -= 1
                    if indegree[dependent_id] == 0:
                        ready.append(dependent_id)

        if visited != len(self.tasks):
            cycle_at = next(
                task_id for task_id, degree in indegree.items() if degree > 0
            )
            raise PlanValidationError(f"Dependency cycle detected at task: {cycle_at}")
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
