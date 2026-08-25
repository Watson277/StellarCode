"""Planner/Worker/Reviewer orchestration over shared tools and project memory."""

from __future__ import annotations

import json
import queue
import re
import threading
import time
import uuid
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from stellarcode.agent import ChatClient
from stellarcode.cancellation import TaskCancelledError, raise_if_cancelled
from stellarcode.memory import MemoryManager
from stellarcode.multi_agent.message import AgentMessage, MessageType
from stellarcode.multi_agent.message_bus import FileMessageBus, MessageBusError
from stellarcode.multi_agent.role import AgentRole
from stellarcode.multi_agent.sub_agent import SubAgent
from stellarcode.plan import ExecutionPlan, PlanValidationError, Planner, Task, TaskStatus
from stellarcode.prompt import PromptAssembler
from stellarcode.skill import SkillContextBuffer, SkillRegistry
from stellarcode.tools import ToolRegistry


class MultiAgentError(RuntimeError):
    pass


@dataclass(frozen=True)
class StepExecutionResult:
    task_id: str
    worker_name: str
    success: bool
    result: str = ""
    error: str = ""
    approved: bool | None = None
    review_feedback: str = ""
    retries: int = 0


class AgentOrchestrator:
    """Coordinates planner, worker, and reviewer agents over an execution DAG."""

    def __init__(
        self,
        llm_client: ChatClient,
        tool_registry: ToolRegistry,
        memory_manager: MemoryManager | None = None,
        worker_count: int = 2,
        max_retries_per_step: int = 2,
        max_iterations_per_agent: int = 6,
        progress_callback: Callable[[str], None] | None = None,
        skill_registry: SkillRegistry | None = None,
        workspace: str | Path | None = None,
        context_window: int = 200_000,
        rag_auto_retrieval: bool | None = True,
        message_bus_dir: str | Path | None = None,
        event_callback: Callable[[str, dict[str, Any]], None] | None = None,
        prompt_assembler: PromptAssembler | None = None,
    ) -> None:
        if worker_count < 1:
            raise ValueError("worker_count must be at least 1.")
        if max_retries_per_step < 0:
            raise ValueError("max_retries_per_step cannot be negative.")

        self.llm_client = llm_client
        self.tool_registry = tool_registry
        self.memory_manager = memory_manager
        self.max_retries_per_step = max_retries_per_step
        self.max_iterations_per_agent = max_iterations_per_agent
        self.progress_callback = progress_callback
        self.skill_registry = skill_registry
        self.workspace = Path(workspace or ".").resolve()
        self.context_window = context_window
        self.rag_auto_retrieval = rag_auto_retrieval
        self.message_bus_dir = Path(
            message_bus_dir or self.workspace / ".stellarcode" / "team-message-bus"
        ).resolve()
        # Team-mode events are deliberately separate from the normal Agent events.
        # They describe visible collaboration (messages, tools, and status), never
        # private chain-of-thought content.
        self.event_callback = event_callback
        self.prompt_assembler = prompt_assembler or PromptAssembler()
        self.plan_parser = Planner(
            llm_client,
            self.workspace,
            prompt_assembler=self.prompt_assembler,
        )
        self.planner = self._new_sub_agent("planner", AgentRole.PLANNER)
        self.workers = [
            self._new_sub_agent(f"worker-{index}", AgentRole.WORKER)
            for index in range(1, worker_count + 1)
        ]
        self.reviewer = self._new_sub_agent("reviewer", AgentRole.REVIEWER)
        self.last_plan: ExecutionPlan | None = None
        self.last_step_results: dict[str, StepExecutionResult] = {}
        self.last_message_bus_path: Path | None = None
        self._worker_cursor = 0
        self._active_message_bus: FileMessageBus | None = None

    def run(
        self,
        user_input: str,
        cancellation_event: threading.Event | None = None,
    ) -> str:
        raise_if_cancelled(cancellation_event)
        self._start_message_bus_run()
        self._emit_team_event(
            "team.run.started",
            {
                "run_id": self._team_run_id(),
                "worker_count": len(self.workers),
            },
        )
        if self.memory_manager:
            self.memory_manager.add_user_message(user_input)

        try:
            plan = self.create_plan(user_input, cancellation_event)
        except (MultiAgentError, PlanValidationError, json.JSONDecodeError) as exc:
            result = f"Multi-Agent planning failed: {exc}"
            self._emit_team_event(
                "team.run.failed",
                {"run_id": self._team_run_id(), "message": result},
            )
            if self.memory_manager:
                self.memory_manager.add_assistant_message(result)
            return result

        try:
            result = self.execute_plan(plan, cancellation_event)
        except TaskCancelledError:
            self._emit_team_event(
                "team.run.failed",
                {"run_id": self._team_run_id(), "message": "Team task was cancelled."},
            )
            raise
        self._emit_team_event(
            "team.run.completed" if not plan.has_failed() else "team.run.failed",
            {
                "run_id": self._team_run_id(),
                "message": result if plan.has_failed() else "Team execution completed.",
            },
        )
        if self.memory_manager:
            self.memory_manager.add_assistant_message(result)
        return result

    def create_plan(
        self,
        user_input: str,
        cancellation_event: threading.Event | None = None,
    ) -> ExecutionPlan:
        self._ensure_message_bus()
        self._emit("Multi-Agent phase 1/2: planner is creating the execution DAG...")
        task = AgentMessage.task(
            "orchestrator",
            f"Create an execution plan for this user goal:\n{user_input}",
        )
        try:
            response = self._execute_agent_via_bus(
                self.planner,
                task,
                task_id="planning",
                cancellation_event=cancellation_event,
            )
        finally:
            self.planner.clear_history()

        if response.type == MessageType.ERROR:
            raise MultiAgentError(response.content)
        if not response.content.strip():
            raise MultiAgentError("planner returned an empty result.")

        plan = self.plan_parser.parse_plan(user_input, response.content)
        self.last_plan = plan
        self._emit(plan.visualize())
        return plan

    def preview_plan(self, user_input: str) -> str:
        return self.create_plan(user_input).visualize()

    def execute_plan(
        self,
        plan: ExecutionPlan,
        cancellation_event: threading.Event | None = None,
    ) -> str:
        raise_if_cancelled(cancellation_event)
        self._ensure_message_bus()
        self.last_plan = plan
        self.last_step_results = {}
        try:
            plan.compute_execution_order()
        except PlanValidationError as exc:
            plan.mark_failed()
            return f"Multi-Agent plan validation failed: {exc}"

        plan.mark_started()
        self._emit("Multi-Agent phase 2/2: workers are executing and reviewers are checking...")
        batch_index = 0

        while True:
            raise_if_cancelled(cancellation_event)
            batch = plan.executable_tasks()
            if not batch:
                break
            batch_index += 1
            self._emit(
                f"Batch {batch_index}: {len(batch)} executable step(s), "
                f"up to {min(len(batch), len(self.workers))} worker(s) in parallel."
            )

            for task in batch:
                task.mark_started()
            outcomes = self._run_batch(plan, batch, cancellation_event)
            for task in batch:
                outcome = outcomes[task.id]
                self.last_step_results[task.id] = outcome
                if outcome.success:
                    task.mark_completed(outcome.result)
                    review = "approved" if outcome.approved else "kept after review warning"
                    if outcome.approved is None:
                        review = "kept because review was unavailable"
                    self._emit(
                        f"{task.id} completed by {outcome.worker_name}; "
                        f"review={review}; retries={outcome.retries}."
                    )
                else:
                    task.mark_failed(outcome.error)
                    plan.skip_blocked_tasks(task.id)
                    self._emit(f"{task.id} failed: {outcome.error}")

        for task in plan.tasks.values():
            if task.status == TaskStatus.PENDING:
                task.mark_skipped("No executable dependency path remained.")

        if plan.has_failed():
            plan.mark_failed()
        else:
            plan.mark_completed()
        return self._build_final_result(plan)

    def reset(self) -> None:
        self.planner.clear_history()
        self.reviewer.clear_history()
        for worker in self.workers:
            worker.clear_history()
        self.last_plan = None
        self.last_step_results = {}
        self._active_message_bus = None

    def parse_review_approval(self, review_content: str | None) -> bool:
        if not review_content or not review_content.strip():
            return False
        try:
            data = json.loads(_extract_json(review_content))
            if not isinstance(data, dict) or "approved" not in data:
                return False
            return data["approved"] is True
        except (json.JSONDecodeError, PlanValidationError):
            lower = review_content.lower()
            negative = (
                "not approved",
                "rejected",
                "failed review",
                "未通过",
                "不通过",
                "不合格",
                "有问题",
            )
            positive = ("approved", "passed review", "通过", "合格")
            if any(keyword in lower for keyword in negative):
                return False
            return any(keyword in lower for keyword in positive)

    def parse_review_issues(self, review_content: str | None) -> str:
        if not review_content or not review_content.strip():
            return "Reviewer returned no usable feedback."
        try:
            data = json.loads(_extract_json(review_content))
            if not isinstance(data, dict):
                raise ValueError
            for field in ("issues", "suggestions"):
                values = data.get(field)
                if isinstance(values, list) and values:
                    return "\n".join(f"- {value}" for value in values)
            summary = str(data.get("summary") or "").strip()
            if summary:
                return summary
        except (json.JSONDecodeError, PlanValidationError, ValueError):
            pass
        return "Review was not approved; improve the execution result."

    def _run_batch(
        self,
        plan: ExecutionPlan,
        batch: list[Task],
        cancellation_event: threading.Event | None = None,
    ) -> dict[str, StepExecutionResult]:
        contexts = {task.id: self._build_step_context(plan, task) for task in batch}
        if len(batch) == 1:
            worker = self.workers[self._worker_cursor % len(self.workers)]
            self._worker_cursor += 1
            reviewer = self.reviewer
            try:
                worker.clear_history()
                return {
                    batch[0].id: self._run_step(
                        batch[0],
                        worker,
                        reviewer,
                        contexts[batch[0].id],
                        cancellation_event,
                    )
                }
            finally:
                worker.clear_history()
                reviewer.clear_history()

        worker_pool: queue.Queue[SubAgent] = queue.Queue()
        for worker in self.workers:
            worker_pool.put(worker)

        def run_parallel(task: Task) -> StepExecutionResult:
            worker = worker_pool.get()
            reviewer = self._new_sub_agent(f"reviewer-{task.id}", AgentRole.REVIEWER)
            try:
                worker.clear_history()
                return self._run_step(
                    task,
                    worker,
                    reviewer,
                    contexts[task.id],
                    cancellation_event,
                )
            except TaskCancelledError:
                raise
            except Exception as exc:
                return StepExecutionResult(
                    task_id=task.id,
                    worker_name=worker.name,
                    success=False,
                    error=f"Parallel step failed: {exc}",
                )
            finally:
                worker.clear_history()
                reviewer.clear_history()
                worker_pool.put(worker)

        parallelism = min(len(batch), len(self.workers))
        with ThreadPoolExecutor(
            max_workers=parallelism,
            thread_name_prefix="stellarcode-team",
        ) as executor:
            futures = {
                task.id: executor.submit(copy_context().run, run_parallel, task) for task in batch
            }
            return {task.id: futures[task.id].result() for task in batch}

    def _run_step(
        self,
        task: Task,
        worker: SubAgent,
        reviewer: SubAgent,
        context: str,
        cancellation_event: threading.Event | None = None,
    ) -> StepExecutionResult:
        raise_if_cancelled(cancellation_event)
        task_message = AgentMessage.task("orchestrator", task.description)
        worker_result = self._execute_agent_via_bus(
            worker,
            task_message,
            task_id=task.id,
            context=context,
            cancellation_event=cancellation_event,
        )
        if worker_result.type == MessageType.ERROR:
            return StepExecutionResult(
                task_id=task.id,
                worker_name=worker.name,
                success=False,
                error=worker_result.content,
            )
        if not worker_result.content.strip():
            return StepExecutionResult(
                task_id=task.id,
                worker_name=worker.name,
                success=False,
                error="Worker returned an empty result.",
            )

        accepted_result = worker_result.content
        review = self._review_via_bus(
            reviewer,
            task,
            accepted_result,
            cancellation_event,
        )
        reviewer.clear_history()
        if review.type == MessageType.ERROR:
            return StepExecutionResult(
                task_id=task.id,
                worker_name=worker.name,
                success=True,
                result=accepted_result,
                approved=None,
                review_feedback=review.content,
            )

        approved = self.parse_review_approval(review.content)
        feedback = self.parse_review_issues(review.content)
        retries = 0

        while not approved and retries < self.max_retries_per_step:
            raise_if_cancelled(cancellation_event)
            retries += 1
            retry_context = (
                f"{context}\n\nThe previous result was rejected by the reviewer.\n"
                f"Review feedback:\n{feedback}"
            )
            retry_result = self._execute_agent_via_bus(
                worker,
                task_message,
                task_id=task.id,
                context=retry_context,
                cancellation_event=cancellation_event,
            )
            if retry_result.type == MessageType.ERROR:
                feedback = retry_result.content
                continue
            if not retry_result.content.strip():
                feedback = "Worker returned an empty result during retry."
                continue

            accepted_result = retry_result.content
            review = self._review_via_bus(
                reviewer,
                task,
                accepted_result,
                cancellation_event,
            )
            reviewer.clear_history()
            if review.type == MessageType.ERROR:
                return StepExecutionResult(
                    task_id=task.id,
                    worker_name=worker.name,
                    success=True,
                    result=accepted_result,
                    approved=None,
                    review_feedback=review.content,
                    retries=retries,
                )
            approved = self.parse_review_approval(review.content)
            feedback = self.parse_review_issues(review.content)

        return StepExecutionResult(
            task_id=task.id,
            worker_name=worker.name,
            success=True,
            result=accepted_result,
            approved=approved,
            review_feedback="" if approved else feedback,
            retries=retries,
        )

    def _review_via_bus(
        self,
        reviewer: SubAgent,
        task: Task,
        execution_result: str,
        cancellation_event: threading.Event | None,
    ) -> AgentMessage:
        """Ask a reviewer through its mailbox rather than by direct peer hand-off."""
        review_task = AgentMessage.task(
            "orchestrator",
            f"Original task:\n{task.description}\n\nExecution result:\n{execution_result}",
        )
        return self._execute_agent_via_bus(
            reviewer,
            review_task,
            task_id=task.id,
            cancellation_event=cancellation_event,
            message_kind="review_request",
        )

    def _execute_agent_via_bus(
        self,
        agent: SubAgent,
        task: AgentMessage,
        *,
        task_id: str,
        cancellation_event: threading.Event | None,
        context: str = "",
        message_kind: str = "task",
    ) -> AgentMessage:
        """Produce one request, let its recipient consume it, then consume its reply.

        The Team execution threads deliberately run this consumer loop locally for
        now.  The durable mailbox protocol is independent of the threads, so a
        later worker process can use the exact same send/claim/ack contract.
        """
        bus = self._ensure_message_bus()
        request = bus.send(
            sender="lead",
            recipient=agent.name,
            kind=message_kind,
            payload={"content": task.content, "context": context},
            task_id=task_id,
        )
        self._emit_team_event(
            "team.agent.message",
            {
                "run_id": self._team_run_id(),
                "agent_name": agent.name,
                "agent_role": agent.role.value.lower(),
                "team_task_id": task_id,
                "direction": "inbound",
                "message_kind": message_kind,
                "content": _team_event_text(task.content),
            },
        )
        self._emit_team_event(
            "team.agent.status",
            {
                "run_id": self._team_run_id(),
                "agent_name": agent.name,
                "agent_role": agent.role.value.lower(),
                "team_task_id": task_id,
                "status": "queued",
            },
        )
        claimed = bus.claim_matching(
            agent.name,
            consumer_id=agent.name,
            predicate=lambda queued: queued.id == request.id,
        )
        if claimed is None:
            raise MultiAgentError(f"MessageBus could not deliver {request.id} to {agent.name}.")

        try:
            self._emit_team_event(
                "team.agent.status",
                {
                    "run_id": self._team_run_id(),
                    "agent_name": agent.name,
                    "agent_role": agent.role.value.lower(),
                    "team_task_id": task_id,
                    "status": "working",
                },
            )
            queued_content = str(claimed.message.payload.get("content") or "")
            queued_context = str(claimed.message.payload.get("context") or "")
            delivered_task = AgentMessage.task(claimed.message.sender, queued_content)
            if queued_context:
                response = agent.execute_with_context(
                    delivered_task,
                    queued_context,
                    cancellation_event,
                    team_task_id=task_id,
                )
            else:
                response = agent.execute(
                    delivered_task,
                    cancellation_event,
                    team_task_id=task_id,
                )
        except TaskCancelledError:
            bus.release(claimed)
            raise
        except Exception as exc:
            response = AgentMessage.error(agent.name, agent.role, f"Agent consumer failed: {exc}")

        self._emit_team_event(
            "team.agent.message",
            {
                "run_id": self._team_run_id(),
                "agent_name": agent.name,
                "agent_role": agent.role.value.lower(),
                "team_task_id": task_id,
                "direction": "outbound",
                "message_kind": "error" if response.type == MessageType.ERROR else "result",
                "content": _team_event_text(response.content),
            },
        )
        self._emit_team_event(
            "team.agent.status",
            {
                "run_id": self._team_run_id(),
                "agent_name": agent.name,
                "agent_role": agent.role.value.lower(),
                "team_task_id": task_id,
                "status": "failed" if response.type == MessageType.ERROR else "completed",
            },
        )

        reply_kind = f"{message_kind}_result"
        bus.send(
            sender=agent.name,
            recipient="lead",
            kind=reply_kind,
            payload={"agent_message": _serialize_agent_message(response)},
            task_id=task_id,
            correlation_id=request.correlation_id,
            parent_message_id=request.id,
        )
        bus.acknowledge(claimed)
        lead_reply = bus.claim_matching(
            "lead",
            consumer_id="lead",
            predicate=lambda queued: (
                queued.kind == reply_kind
                and queued.correlation_id == request.correlation_id
                and queued.parent_message_id == request.id
            ),
        )
        if lead_reply is None:
            raise MultiAgentError(f"MessageBus did not return a reply for {request.id}.")
        try:
            raw_reply = lead_reply.message.payload.get("agent_message")
            if not isinstance(raw_reply, dict):
                raise MessageBusError("Agent reply did not include a structured AgentMessage.")
            decoded = _deserialize_agent_message(raw_reply)
        except Exception:
            # Keep malformed replies eligible for retry/dead-letter inspection.
            bus.release(lead_reply)
            raise
        else:
            bus.acknowledge(lead_reply)
            return decoded

    def _start_message_bus_run(self) -> FileMessageBus:
        """Start a fresh durable mailbox namespace for one Team-mode user task."""
        run_id = f"team-{int(time.time() * 1000)}-{uuid.uuid4().hex[:12]}"
        bus = FileMessageBus(self.message_bus_dir / run_id)
        self._active_message_bus = bus
        self.last_message_bus_path = bus.root
        return bus

    def _ensure_message_bus(self) -> FileMessageBus:
        if self._active_message_bus is None:
            return self._start_message_bus_run()
        return self._active_message_bus

    def _build_step_context(self, plan: ExecutionPlan, current_task: Task) -> str:
        lines = [f"Overall goal:\n{plan.goal}"]
        for dependency_id in current_task.dependencies:
            dependency = plan.get_task(dependency_id)
            if dependency.status != TaskStatus.COMPLETED:
                continue
            preview = dependency.result
            if len(preview) > 500:
                preview = f"{preview[:500]}..."
            lines.append(
                f"Completed dependency [{dependency.id}]: {dependency.description}\n"
                f"Result: {preview}"
            )
        return "\n\n".join(lines)

    def _build_final_result(self, plan: ExecutionPlan) -> str:
        lines = [
            f"Multi-Agent status: {plan.status.value}",
            f"Goal: {plan.goal}",
            f"Team: 1 planner, {len(self.workers)} worker(s), 1 reviewer",
            "",
            "Step results:",
        ]
        for task_id in plan.execution_order:
            task = plan.get_task(task_id)
            outcome = self.last_step_results.get(task_id)
            worker = outcome.worker_name if outcome else "-"
            lines.append(f"- {task.id} [{task.status.value}] {task.description} (worker: {worker})")
            if task.result:
                lines.append(f"  result: {task.result}")
            if task.error:
                lines.append(f"  error: {task.error}")
            if outcome and outcome.approved is False:
                lines.append(
                    f"  review: not approved after {outcome.retries} retries; "
                    "latest result was kept"
                )
                if outcome.review_feedback:
                    lines.append(f"  feedback: {outcome.review_feedback}")
            elif outcome and outcome.approved is None:
                lines.append("  review: unavailable; worker result was kept")
        return "\n".join(lines)

    def _new_sub_agent(self, name: str, role: AgentRole) -> SubAgent:
        return SubAgent(
            name=name,
            role=role,
            llm_client=self.llm_client,
            tool_registry=self.tool_registry,
            max_iterations=self.max_iterations_per_agent,
            memory_manager=self.memory_manager,
            skill_registry=self.skill_registry,
            skill_context_buffer=SkillContextBuffer(),
            workspace=self.workspace,
            context_window=self.context_window,
            rag_auto_retrieval=self.rag_auto_retrieval,
            event_callback=self._emit_team_event,
            prompt_assembler=self.prompt_assembler,
        )

    def _emit(self, message: str) -> None:
        if self.progress_callback:
            self.progress_callback(message)

    def _emit_team_event(self, event_type: str, data: dict[str, Any]) -> None:
        """Best-effort presentation telemetry for the expandable Team card."""

        if not self.event_callback:
            return
        try:
            self.event_callback(event_type, data)
        except Exception:
            # Team collaboration must never fail because the desktop is offline.
            pass

    def _team_run_id(self) -> str:
        return self.last_message_bus_path.name if self.last_message_bus_path else "team"


def _extract_json(text: str) -> str:
    stripped = text.strip()
    fenced = re.search(r"```(?:json)?\s*(.*?)\s*```", stripped, flags=re.DOTALL)
    if fenced:
        return fenced.group(1).strip()
    start = stripped.find("{")
    end = stripped.rfind("}")
    if start == -1 or end == -1 or end < start:
        raise PlanValidationError("Response does not contain a JSON object.")
    return stripped[start : end + 1]


def _serialize_agent_message(message: AgentMessage) -> dict[str, str | None]:
    """Keep the mailbox payload independent from Python dataclass pickling."""
    return {
        "from_agent": message.from_agent,
        "from_role": message.from_role.value if message.from_role else None,
        "content": message.content,
        "type": message.type.value,
    }


def _deserialize_agent_message(payload: dict[str, object]) -> AgentMessage:
    """Validate an agent reply before the Lead lets it influence the DAG."""
    try:
        role_value = payload.get("from_role")
        role = AgentRole(str(role_value)) if role_value is not None else None
        return AgentMessage(
            from_agent=str(payload["from_agent"]),
            from_role=role,
            content=str(payload["content"]),
            type=MessageType(str(payload["type"])),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise MessageBusError("Malformed AgentMessage payload in Lead mailbox.") from exc


def _team_event_text(value: str, limit: int = 2_400) -> str:
    """Bound UI/journal payloads while retaining readable child-agent dialogue."""

    text = value.strip()
    return text if len(text) <= limit else f"{text[:limit]}\n… [truncated]"
