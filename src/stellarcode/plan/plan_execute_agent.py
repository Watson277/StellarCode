from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
from pathlib import Path
import threading

from stellarcode.agent import Agent, ChatClient
from stellarcode.cancellation import TaskCancelledError, raise_if_cancelled
from stellarcode.memory import MemoryManager
from stellarcode.plan.execution_plan import ExecutionPlan, PlanValidationError
from stellarcode.plan.planner import Planner
from stellarcode.plan.task import Task, TaskStatus
from stellarcode.skill import SkillContextBuffer, SkillRegistry
from stellarcode.tools import ToolRegistry


class PlanExecuteAgent:
    def __init__(
        self,
        llm_client: ChatClient,
        tool_registry: ToolRegistry,
        max_iterations_per_task: int = 6,
        memory_manager: MemoryManager | None = None,
        max_parallel_tasks: int = 4,
        progress_callback: Callable[[str], None] | None = None,
        skill_registry: SkillRegistry | None = None,
        skill_context_buffer: SkillContextBuffer | None = None,
        workspace: str | Path | None = None,
        event_callback: Callable[[str, dict[str, object]], None] | None = None,
        context_window: int = 200_000,
    ) -> None:
        if max_parallel_tasks < 1:
            raise ValueError("max_parallel_tasks must be at least 1.")
        self.llm_client = llm_client
        self.tool_registry = tool_registry
        self.max_iterations_per_task = max_iterations_per_task
        self.memory_manager = memory_manager
        self.max_parallel_tasks = max_parallel_tasks
        self.progress_callback = progress_callback
        self.skill_registry = skill_registry
        self.skill_context_buffer = skill_context_buffer
        self.workspace = Path(workspace or ".").resolve()
        self.planner = Planner(llm_client, self.workspace)
        self.event_callback = event_callback
        self.context_window = context_window

    def run(
        self,
        user_input: str,
        cancellation_event: threading.Event | None = None,
    ) -> str:
        self._emit_progress("[Planner] Creating an execution plan")
        plan = self.planner.create_plan(user_input, cancellation_event)
        return self.execute_plan(plan, cancellation_event)

    def execute_plan(
        self,
        plan: ExecutionPlan,
        cancellation_event: threading.Event | None = None,
    ) -> str:
        raise_if_cancelled(cancellation_event)
        try:
            batches = plan.execution_batches()
            plan.execution_order = [task.id for batch in batches for task in batch]
        except PlanValidationError as exc:
            plan.mark_failed()
            return f"Plan validation failed: {exc}"

        self._emit_event(
            "plan.created",
            {
                "goal": plan.goal,
                "summary": plan.summary,
                "tasks": [
                    {
                        "id": task.id,
                        "description": task.description,
                        "task_type": task.type.value,
                        "dependencies": list(task.dependencies),
                    }
                    for task in plan.tasks.values()
                ],
                "execution_order": list(plan.execution_order),
            },
        )

        plan.mark_started()
        for planned_batch in batches:
            raise_if_cancelled(cancellation_event)
            batch = [task for task in planned_batch if task.is_executable(plan.tasks)]
            for task in planned_batch:
                if task.status == TaskStatus.PENDING and task not in batch:
                    task.mark_skipped("Dependencies are not completed.")
                    self._emit_step_skipped(task)
            if not batch:
                continue

            for task in batch:
                task.mark_started()
                self._emit_event(
                    "plan.step.started",
                    {"step_id": task.id},
                )
            outcomes = self._execute_task_batch(plan, batch, cancellation_event)
            for task, result, error in outcomes:
                if error is None:
                    task.mark_completed(result)
                    self._emit_event(
                        "plan.step.completed",
                        {
                            "step_id": task.id,
                            "result_preview": _truncate_text(result, 600),
                            "retry_count": 0,
                        },
                    )
                else:
                    task.mark_failed(error)
                    self._emit_event(
                        "plan.step.failed",
                        {"step_id": task.id, "error": error},
                    )
                    pending_before = {
                        item.id
                        for item in plan.tasks.values()
                        if item.status == TaskStatus.PENDING
                    }
                    plan.skip_blocked_tasks(task.id)
                    for skipped_id in pending_before:
                        skipped = plan.get_task(skipped_id)
                        if skipped.status == TaskStatus.SKIPPED:
                            self._emit_step_skipped(skipped)

        if plan.has_failed():
            plan.mark_failed()
        else:
            plan.mark_completed()
        return plan.build_result()

    def preview_plan(self, user_input: str) -> str:
        plan = self.planner.create_plan(user_input)
        return plan.visualize()

    def _execute_task(
        self,
        plan: ExecutionPlan,
        task: Task,
        cancellation_event: threading.Event | None = None,
    ) -> str:
        agent = Agent(
            llm_client=self.llm_client,
            tool_registry=self.tool_registry,
            max_iterations=self.max_iterations_per_task,
            memory_manager=self.memory_manager,
            progress_callback=self.progress_callback,
            skill_registry=self.skill_registry,
            skill_context_buffer=self.skill_context_buffer,
            workspace=self.workspace,
            event_callback=self.event_callback,
            context_window=self.context_window,
            llm_operation_name="plan-step",
            stream_output=False,
        )
        return agent.run(_task_prompt(plan, task), cancellation_event)

    def _execute_task_batch(
        self,
        plan: ExecutionPlan,
        tasks: list[Task],
        cancellation_event: threading.Event | None = None,
    ) -> list[tuple[Task, str, str | None]]:
        def execute_task(task: Task) -> str:
            if cancellation_event is None:
                return self._execute_task(plan, task)
            return self._execute_task(plan, task, cancellation_event)

        if len(tasks) == 1:
            task = tasks[0]
            try:
                return [(task, execute_task(task), None)]
            except TaskCancelledError:
                raise
            except Exception as exc:
                return [(task, "", str(exc))]

        parallelism = min(len(tasks), self.max_parallel_tasks)
        with ThreadPoolExecutor(
            max_workers=parallelism,
            thread_name_prefix="stellarcode-plan",
        ) as executor:
            futures = [
                executor.submit(copy_context().run, execute_task, task)
                for task in tasks
            ]
            outcomes = []
            for task, future in zip(tasks, futures):
                try:
                    outcomes.append((task, future.result(), None))
                except TaskCancelledError:
                    raise
                except Exception as exc:
                    outcomes.append((task, "", str(exc)))
            return outcomes

    def _emit_progress(self, message: str) -> None:
        if not self.progress_callback:
            return
        try:
            self.progress_callback(message)
        except Exception:
            pass

    def _emit_event(self, event_type: str, data: dict[str, object]) -> None:
        if not self.event_callback:
            return
        try:
            self.event_callback(event_type, data)
        except Exception:
            pass

    def _emit_step_skipped(self, task: Task) -> None:
        self._emit_event(
            "plan.step.skipped",
            {"step_id": task.id, "reason": task.error or "Dependency was not completed."},
        )


def should_plan(user_input: str) -> bool:
    keywords = ["创建", "实现", "修改", "然后", "接着", "最后", "并且", "测试", "验证"]
    score = sum(1 for keyword in keywords if keyword in user_input)
    return score >= 2 or len(user_input) > 80


def _truncate_text(value: str, limit: int) -> str:
    compact = " ".join(value.split())
    return compact if len(compact) <= limit else f"{compact[:limit]}..."


def _task_prompt(plan: ExecutionPlan, task: Task) -> str:
    dependency_results = []
    for dependency_id in task.dependencies:
        dependency = plan.get_task(dependency_id)
        if dependency.result:
            dependency_results.append(f"{dependency.id}: {dependency.result}")

    dependency_text = "\n".join(dependency_results) or "(none)"
    return f"""Execute one task from a larger plan.

Overall goal:
{plan.goal}

Current task:
- id: {task.id}
- type: {task.type.value}
- description: {task.description}

Completed dependency results:
{dependency_text}

Use available tools when needed. Return a concise task result.
"""
