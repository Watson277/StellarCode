from __future__ import annotations

import json
import re
import uuid
from pathlib import Path

from stellarcode.agent import ChatClient, runtime_context
from stellarcode.image import ImageReferenceParser
from stellarcode.plan.execution_plan import ExecutionPlan, PlanValidationError
from stellarcode.plan.task import Task, TaskType


PLANNING_PROMPT = """You are StellarCode's planner.

Split the user's complex goal into a small executable DAG. Return JSON only.

Available task types:
- PLANNING: clarify strategy or structure.
- FILE_READ: read files for context.
- FILE_WRITE: create or modify files.
- COMMAND: run shell commands, tests, builds, or scripts.
- ANALYSIS: inspect results and make decisions.
- VERIFICATION: check correctness.

Rules:
1. Every task must have an id like task_1, task_2.
2. dependencies must contain only earlier task ids.
3. Keep the plan focused; prefer 3 to 7 tasks.
4. Include verification when the goal changes code or files.
5. Return exactly this JSON shape:
{
  "summary": "short plan summary",
  "tasks": [
    {
      "id": "task_1",
      "description": "task description",
      "type": "FILE_READ",
      "dependencies": []
    }
  ]
}
"""


class Planner:
    def __init__(
        self,
        llm_client: ChatClient,
        workspace: str | Path | None = None,
    ) -> None:
        self.llm_client = llm_client
        self.image_parser = ImageReferenceParser(workspace or ".")

    def create_plan(self, goal: str) -> ExecutionPlan:
        messages = [
            {
                "role": "system",
                "content": f"{PLANNING_PROMPT}\n\n{runtime_context()}",
            },
            self.image_parser.user_message(f"Create an execution plan for this goal:\n{goal}"),
        ]
        response = self.llm_client.chat(messages, tools=None)
        return self.parse_plan(goal, str(response.get("content") or ""))

    def parse_plan(self, goal: str, raw_output: str) -> ExecutionPlan:
        data = json.loads(_extract_json(raw_output))
        if not isinstance(data, dict):
            raise PlanValidationError("Planner output must be a JSON object.")

        raw_tasks = data.get("tasks")
        if not isinstance(raw_tasks, list) or not raw_tasks:
            raw_tasks = data.get("steps")
        if not isinstance(raw_tasks, list) or not raw_tasks:
            raise PlanValidationError(
                "Planner output must include a non-empty tasks or steps list."
            )

        plan = ExecutionPlan(
            id=f"plan_{uuid.uuid4().hex[:8]}",
            goal=goal,
            summary=str(data.get("summary") or ""),
        )

        id_mapping: dict[str, str] = {}
        for index, item in enumerate(raw_tasks, start=1):
            if not isinstance(item, dict):
                raise PlanValidationError("Each task must be a JSON object.")
            original_id = str(item.get("id") or f"task_{index}")
            normalized_id = f"task_{index}"
            id_mapping[original_id] = normalized_id

            task_type = _parse_task_type(str(item.get("type") or "ANALYSIS"))
            task = Task(
                id=normalized_id,
                description=str(item.get("description") or "").strip(),
                type=task_type,
            )
            if not task.description:
                raise PlanValidationError(f"Task {normalized_id} has empty description.")
            plan.add_task(task)

        for index, item in enumerate(raw_tasks, start=1):
            task = plan.get_task(f"task_{index}")
            raw_dependencies = item.get("dependencies") or []
            if not isinstance(raw_dependencies, list):
                raise PlanValidationError(f"Task {task.id} dependencies must be a list.")
            task.dependencies = [
                id_mapping.get(str(dep), str(dep)) for dep in raw_dependencies
            ]

        plan.compute_execution_order()
        return plan


def _extract_json(text: str) -> str:
    stripped = text.strip()
    fenced = re.search(r"```(?:json)?\s*(.*?)\s*```", stripped, flags=re.DOTALL)
    if fenced:
        return fenced.group(1).strip()

    start = stripped.find("{")
    end = stripped.rfind("}")
    if start == -1 or end == -1 or end < start:
        raise PlanValidationError("Planner output does not contain a JSON object.")
    return stripped[start : end + 1]


def _parse_task_type(value: str) -> TaskType:
    normalized = value.strip().upper()
    try:
        return TaskType(normalized)
    except ValueError:
        return TaskType.ANALYSIS
