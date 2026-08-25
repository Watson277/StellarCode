"""Task node status and dependency data used by Plan-and-Execute scheduling."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum


class TaskType(str, Enum):
    PLANNING = "PLANNING"
    FILE_READ = "FILE_READ"
    FILE_WRITE = "FILE_WRITE"
    COMMAND = "COMMAND"
    ANALYSIS = "ANALYSIS"
    VERIFICATION = "VERIFICATION"


class TaskStatus(str, Enum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    SKIPPED = "SKIPPED"


@dataclass
class Task:
    id: str
    description: str
    type: TaskType
    dependencies: list[str] = field(default_factory=list)
    status: TaskStatus = TaskStatus.PENDING
    result: str = ""
    error: str = ""
    start_time: float | None = None
    end_time: float | None = None

    def is_executable(self, tasks: dict[str, "Task"]) -> bool:
        if self.status != TaskStatus.PENDING:
            return False
        return all(
            dep_id in tasks and tasks[dep_id].status == TaskStatus.COMPLETED
            for dep_id in self.dependencies
        )

    def mark_started(self) -> None:
        self.status = TaskStatus.RUNNING
        self.start_time = time.time()

    def mark_completed(self, result: str) -> None:
        self.status = TaskStatus.COMPLETED
        self.result = result
        self.end_time = time.time()

    def mark_failed(self, error: str) -> None:
        self.status = TaskStatus.FAILED
        self.error = error
        self.end_time = time.time()

    def mark_skipped(self, error: str) -> None:
        self.status = TaskStatus.SKIPPED
        self.error = error
        self.end_time = time.time()

