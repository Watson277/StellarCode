"""In-process task routing state for concurrent projects and conversations.

This state machine is intentionally non-durable: checkpoints and journals are the
recovery source of truth after a Sidecar restart.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, replace
from typing import Any, Literal


TaskRoutePhase = Literal["accepted", "running", "cancelling", "finalizing"]


@dataclass(frozen=True)
class TaskRoute:
    runtime: Any
    project_id: str
    session_id: str
    phase: TaskRoutePhase = "accepted"


class RuntimeTaskStateMachine:
    """Authoritative in-process task routing and lifecycle transitions.

    Conversation persistence/checkpoints remain authoritative across process
    restarts. This state machine owns only the live Sidecar routing layer, so
    request handlers no longer mutate parallel task/session dictionaries by
    hand.
    """

    _ALLOWED_TRANSITIONS: dict[TaskRoutePhase, set[TaskRoutePhase]] = {
        "accepted": {"running", "cancelling", "finalizing"},
        "running": {"cancelling", "finalizing"},
        "cancelling": {"finalizing"},
        "finalizing": set(),
    }

    def __init__(self, lock: threading.RLock | None = None) -> None:
        self._lock = lock or threading.RLock()
        self.routes: dict[str, TaskRoute] = {}
        self.tasks_by_session: dict[str, str] = {}

    def register(
        self,
        runtime: Any,
        project_id: str,
        session_id: str,
        task_id: str,
        *,
        phase: TaskRoutePhase = "accepted",
    ) -> TaskRoute:
        if not task_id or not session_id or not project_id:
            raise ValueError("task_id, session_id, and project_id must not be empty")
        with self._lock:
            existing_task = self.tasks_by_session.get(session_id)
            if existing_task and existing_task != task_id:
                raise RuntimeError("this conversation already has a running task")
            existing_route = self.routes.get(task_id)
            if existing_route is not None:
                if (
                    existing_route.runtime is not runtime
                    or existing_route.project_id != project_id
                    or existing_route.session_id != session_id
                ):
                    raise RuntimeError("task id is already routed to another conversation")
                if existing_route.phase != phase:
                    return self.transition(task_id, phase)
                return existing_route
            route = TaskRoute(runtime, project_id, session_id, phase)
            self.routes[task_id] = route
            self.tasks_by_session[session_id] = task_id
            return route

    def transition(self, task_id: str, phase: TaskRoutePhase) -> TaskRoute:
        with self._lock:
            route = self.routes.get(task_id)
            if route is None:
                raise RuntimeError("task route is not active")
            if route.phase == phase:
                return route
            if phase not in self._ALLOWED_TRANSITIONS[route.phase]:
                raise RuntimeError(
                    f"invalid task route transition: {route.phase} -> {phase}"
                )
            updated = replace(route, phase=phase)
            self.routes[task_id] = updated
            return updated

    def release(self, task_id: str) -> TaskRoute | None:
        with self._lock:
            route = self.routes.pop(task_id, None)
            if route and self.tasks_by_session.get(route.session_id) == task_id:
                self.tasks_by_session.pop(route.session_id, None)
            return route

    def route(self, task_id: str) -> TaskRoute | None:
        with self._lock:
            return self.routes.get(task_id)

    def task_for_session(self, session_id: str) -> str | None:
        with self._lock:
            return self.tasks_by_session.get(session_id)

    def has_project_tasks(self, project_id: str) -> bool:
        with self._lock:
            return any(route.project_id == project_id for route in self.routes.values())

    def active_task_ids(self) -> set[str]:
        with self._lock:
            return set(self.routes)

    def routes_for_project(self, project_id: str) -> list[tuple[str, TaskRoute]]:
        with self._lock:
            return [
                (task_id, route)
                for task_id, route in self.routes.items()
                if route.project_id == project_id
            ]
