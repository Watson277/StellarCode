from __future__ import annotations

from enum import Enum


class AgentRole(str, Enum):
    PLANNER = "PLANNER"
    WORKER = "WORKER"
    REVIEWER = "REVIEWER"

    @property
    def display_name(self) -> str:
        return {
            AgentRole.PLANNER: "Planner",
            AgentRole.WORKER: "Worker",
            AgentRole.REVIEWER: "Reviewer",
        }[self]

    @property
    def description(self) -> str:
        return {
            AgentRole.PLANNER: "Breaks a complex goal into an executable DAG.",
            AgentRole.WORKER: "Executes one concrete step and may call tools.",
            AgentRole.REVIEWER: "Checks a step result and returns structured feedback.",
        }[self]
