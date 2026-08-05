from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from collections.abc import Callable
from pathlib import Path

from stellarcode.agent import Agent, ChatClient
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

    def run(self, user_input: str) -> str:
        plan = self.planner.create_plan(user_input)
        return self.execute_plan(plan)

    def execute_plan(self, plan: ExecutionPlan) -> str:
        try:
            batches = plan.execution_batches()
            plan.execution_order = [task.id for batch in batches for task in batch]
        except PlanValidationError as exc:
            plan.mark_failed()
            return f"Plan validation failed: {exc}"

        plan.mark_started()
        for planned_batch in batches:
            batch = [task for task in planned_batch if task.is_executable(plan.tasks)]
            for task in planned_batch:
                if task.status == TaskStatus.PENDING and task not in batch:
                    task.mark_skipped("Dependencies are not completed.")
            if not batch:
                continue

            for task in batch:
                task.mark_started()
            outcomes = self._execute_task_batch(plan, batch)
            for task, result, error in outcomes:
                if error is None:
                    task.mark_completed(result)
                else:
                    task.mark_failed(error)
                    plan.skip_blocked_tasks(task.id)

        if plan.has_failed():
            plan.mark_failed()
        else:
            plan.mark_completed()
        return plan.build_result()

    def preview_plan(self, user_input: str) -> str:
        plan = self.planner.create_plan(user_input)
        return plan.visualize()

    def _execute_task(self, plan: ExecutionPlan, task: Task) -> str:
        agent = Agent(
            llm_client=self.llm_client,
            tool_registry=self.tool_registry,
            max_iterations=self.max_iterations_per_task,
            memory_manager=self.memory_manager,
            progress_callback=self.progress_callback,
            skill_registry=self.skill_registry,
            skill_context_buffer=self.skill_context_buffer,
            workspace=self.workspace,
        )
        return agent.run(_task_prompt(plan, task))

    def _execute_task_batch(
        self,
        plan: ExecutionPlan,
        tasks: list[Task],
    ) -> list[tuple[Task, str, str | None]]:
        if len(tasks) == 1:
            task = tasks[0]
            try:
                return [(task, self._execute_task(plan, task), None)]
            except Exception as exc:
                return [(task, "", str(exc))]

        parallelism = min(len(tasks), self.max_parallel_tasks)
        with ThreadPoolExecutor(
            max_workers=parallelism,
            thread_name_prefix="stellarcode-plan",
        ) as executor:
            futures = [executor.submit(self._execute_task, plan, task) for task in tasks]
            outcomes = []
            for task, future in zip(tasks, futures):
                try:
                    outcomes.append((task, future.result(), None))
                except Exception as exc:
                    outcomes.append((task, "", str(exc)))
            return outcomes


def should_plan(user_input: str) -> bool:
    keywords = ["创建", "实现", "修改", "然后", "接着", "最后", "并且", "测试", "验证"]
    score = sum(1 for keyword in keywords if keyword in user_input)
    return score >= 2 or len(user_input) > 80


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
