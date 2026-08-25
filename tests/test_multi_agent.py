from __future__ import annotations

import threading
from collections.abc import Callable
from typing import Any

from stellarcode.memory import MemoryManager
from stellarcode.multi_agent import (
    AgentMessage,
    AgentOrchestrator,
    AgentRole,
    FileMessageBus,
    MessageType,
    SubAgent,
)
from stellarcode.plan import ExecutionPlan, PlanStatus, Task, TaskType
from stellarcode.tools import ToolDefinition, ToolRegistry, build_default_registry


class DispatchClient:
    def __init__(
        self,
        handler: Callable[[list[dict[str, Any]], list[dict[str, Any]] | None], str],
    ) -> None:
        self.handler = handler
        self.calls: list[tuple[list[dict[str, Any]], list[dict[str, Any]] | None]] = []
        self.lock = threading.Lock()

    def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        temperature: float = 0.2,
    ) -> dict[str, Any]:
        copied_messages = [dict(message) for message in messages]
        with self.lock:
            self.calls.append((copied_messages, tools))
        return {
            "role": "assistant",
            "content": self.handler(copied_messages, tools),
        }


def test_agent_roles_and_message_factories():
    task = AgentMessage.task("orchestrator", "do work")
    result = AgentMessage.result("worker-1", AgentRole.WORKER, "done")
    rejection = AgentMessage.rejection("reviewer", "fix it")

    assert AgentRole.PLANNER.display_name == "Planner"
    assert task.type == MessageType.TASK
    assert task.from_role is None
    assert result.from_role == AgentRole.WORKER
    assert rejection.type == MessageType.REJECTION


def test_only_worker_receives_tool_schemas(tmp_path):
    client = DispatchClient(lambda _messages, _tools: "done")
    registry = build_default_registry(tmp_path)
    planner = SubAgent("planner", AgentRole.PLANNER, client, registry)
    worker = SubAgent("worker", AgentRole.WORKER, client, registry)
    reviewer = SubAgent("reviewer", AgentRole.REVIEWER, client, registry)

    planner.execute(AgentMessage.task("orchestrator", "plan"))
    worker.execute(AgentMessage.task("orchestrator", "work"))
    reviewer.execute(AgentMessage.task("orchestrator", "review"))

    assert client.calls[0][1] is None
    assert client.calls[1][1]
    assert client.calls[2][1] is None


def test_team_sub_agent_receives_memory_as_user_context_not_system(tmp_path):
    client = DispatchClient(lambda _messages, _tools: "done")
    manager = MemoryManager(storage_dir=tmp_path / "memory")
    manager.save_fact("Python runtime uses version 3.12")
    worker = SubAgent(
        "worker",
        AgentRole.WORKER,
        client,
        build_default_registry(tmp_path),
        memory_manager=manager,
    )

    worker.execute(AgentMessage.task("orchestrator", "inspect Python runtime"))

    messages, _tools = client.calls[0]
    assert "version 3.12" not in messages[0]["content"]
    assert '"kind":"retrieved_memory"' in messages[-2]["content"]
    assert messages[-1]["role"] == "user"
    assert all("_stellarcode_context" not in message for message in messages)


def test_worker_executes_same_round_tool_calls_in_parallel():
    barrier = threading.Barrier(2)
    registry = ToolRegistry()

    def wait_and_return(value: str) -> str:
        barrier.wait(timeout=2)
        return value

    registry.register(
        ToolDefinition(
            "barrier",
            "Test helper.",
            {"type": "object"},
            wait_and_return,
        )
    )

    class ParallelWorkerClient:
        def __init__(self) -> None:
            self.calls = 0

        def chat(self, messages, tools=None, temperature=0.2):
            self.calls += 1
            if self.calls == 1:
                return {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {
                            "id": "one",
                            "type": "function",
                            "function": {
                                "name": "barrier",
                                "arguments": '{"value":"one"}',
                            },
                        },
                        {
                            "id": "two",
                            "type": "function",
                            "function": {
                                "name": "barrier",
                                "arguments": '{"value":"two"}',
                            },
                        },
                    ],
                }
            assert [message["content"] for message in messages[-2:]] == ["one", "two"]
            return {"role": "assistant", "content": "worker complete"}

    worker = SubAgent(
        "worker-1",
        AgentRole.WORKER,
        ParallelWorkerClient(),
        registry,
    )

    result = worker.execute(AgentMessage.task("orchestrator", "run both"))

    assert result.type == MessageType.RESULT
    assert result.content == "worker complete"


def test_rejected_step_retries_with_feedback_until_approved(tmp_path):
    worker_calls = 0

    def handler(messages: list[dict[str, Any]], _tools: list[dict[str, Any]] | None) -> str:
        nonlocal worker_calls
        system = messages[0]["content"]
        user = messages[-1]["content"]
        if "planner in a multi-agent" in system:
            return """
{
  "summary": "single step",
  "steps": [
    {"id": "build", "description": "build feature", "type": "FILE_WRITE", "dependencies": []}
  ]
}
"""
        if "worker in a multi-agent" in system:
            worker_calls += 1
            if worker_calls == 1:
                return "attempt one"
            assert "missing verification" in user
            return "attempt two with verification"
        if "attempt one" in user:
            return """
{"approved": false, "summary": "retry", "issues": ["missing verification"]}
"""
        return '{"approved": true, "summary": "ok", "issues": []}'

    orchestrator = AgentOrchestrator(
        DispatchClient(handler),
        build_default_registry(tmp_path),
        worker_count=1,
    )

    result = orchestrator.run("build and verify a feature")

    assert "Multi-Agent status: COMPLETED" in result
    assert "attempt two with verification" in result
    assert orchestrator.last_step_results["task_1"].approved is True
    assert orchestrator.last_step_results["task_1"].retries == 1


def test_independent_steps_run_on_two_workers_in_parallel(tmp_path):
    barrier = threading.Barrier(2)
    current = 0
    peak = 0
    lock = threading.Lock()

    def handler(messages: list[dict[str, Any]], _tools: list[dict[str, Any]] | None) -> str:
        nonlocal current, peak
        system = messages[0]["content"]
        user = messages[-1]["content"]
        if "worker in a multi-agent" in system:
            with lock:
                current += 1
                peak = max(peak, current)
            barrier.wait(timeout=3)
            with lock:
                current -= 1
            return f"result for {user.split('Current task:')[-1].strip()}"
        return '{"approved": true, "summary": "ok", "issues": []}'

    plan = ExecutionPlan(id="team_plan", goal="parallel work")
    plan.add_task(Task("task_1", "task A", TaskType.ANALYSIS))
    plan.add_task(Task("task_2", "task B", TaskType.ANALYSIS))
    orchestrator = AgentOrchestrator(
        DispatchClient(handler),
        build_default_registry(tmp_path),
        worker_count=2,
    )

    result = orchestrator.execute_plan(plan)

    assert peak == 2
    assert plan.status == PlanStatus.COMPLETED
    assert "task A" in result
    assert "task B" in result
    worker_names = {
        orchestrator.last_step_results["task_1"].worker_name,
        orchestrator.last_step_results["task_2"].worker_name,
    }
    assert worker_names == {"worker-1", "worker-2"}


def test_dependency_result_is_injected_into_next_worker_context(tmp_path):
    worker_inputs: list[str] = []

    def handler(messages: list[dict[str, Any]], _tools: list[dict[str, Any]] | None) -> str:
        system = messages[0]["content"]
        user = messages[-1]["content"]
        if "worker in a multi-agent" in system:
            worker_inputs.append(user)
            if "first task" in user:
                return "dependency output"
            return "used dependency"
        return '{"approved": true, "summary": "ok", "issues": []}'

    plan = ExecutionPlan(id="team_plan", goal="dependent work")
    plan.add_task(Task("task_1", "first task", TaskType.ANALYSIS))
    plan.add_task(
        Task(
            "task_2",
            "second task",
            TaskType.ANALYSIS,
            dependencies=["task_1"],
        )
    )
    orchestrator = AgentOrchestrator(
        DispatchClient(handler),
        build_default_registry(tmp_path),
        worker_count=1,
    )

    orchestrator.execute_plan(plan)

    assert "Completed dependency [task_1]: first task" in worker_inputs[1]
    assert "Result: dependency output" in worker_inputs[1]


def test_team_execution_routes_worker_and_reviewer_replies_through_mailboxes(tmp_path):
    def handler(messages: list[dict[str, Any]], _tools: list[dict[str, Any]] | None) -> str:
        if "worker in a multi-agent" in messages[0]["content"]:
            return "worker completed the assigned step"
        return '{"approved": true, "summary": "reviewed", "issues": []}'

    plan = ExecutionPlan(id="team_plan", goal="route messages")
    plan.add_task(Task("task_1", "complete one step", TaskType.ANALYSIS))
    orchestrator = AgentOrchestrator(
        DispatchClient(handler),
        build_default_registry(tmp_path),
        worker_count=1,
        message_bus_dir=tmp_path / "team-bus",
    )

    result = orchestrator.execute_plan(plan)

    assert "Multi-Agent status: COMPLETED" in result
    assert orchestrator.last_message_bus_path is not None
    bus = FileMessageBus(orchestrator.last_message_bus_path)
    worker_kinds = [message.kind for message in bus.mailbox_messages("worker-1")]
    reviewer_kinds = [message.kind for message in bus.mailbox_messages("reviewer")]
    lead_kinds = [message.kind for message in bus.mailbox_messages("lead")]
    assert worker_kinds == ["task"]
    assert reviewer_kinds == ["review_request"]
    assert lead_kinds == ["task_result", "review_request_result"]
    assert bus.pending_count("lead") == 0


def test_team_emits_structured_sub_agent_dialogue_events(tmp_path):
    events: list[tuple[str, dict[str, Any]]] = []

    def handler(messages: list[dict[str, Any]], _tools: list[dict[str, Any]] | None) -> str:
        system = messages[0]["content"]
        if "planner in a multi-agent" in system:
            return """
{
  "summary": "one step",
  "steps": [
    {"id": "task_1", "description": "inspect the project", "type": "ANALYSIS", "dependencies": []}
  ]
}
"""
        if "worker in a multi-agent" in system:
            return "inspection complete"
        return '{"approved": true, "summary": "reviewed", "issues": []}'

    orchestrator = AgentOrchestrator(
        DispatchClient(handler),
        build_default_registry(tmp_path),
        worker_count=1,
        event_callback=lambda event_type, data: events.append((event_type, data)),
    )

    result = orchestrator.run("inspect the project")

    assert "Multi-Agent status: COMPLETED" in result
    event_types = [event_type for event_type, _data in events]
    assert event_types[0] == "team.run.started"
    assert "team.agent.message" in event_types
    assert "team.agent.status" in event_types
    assert event_types[-1] == "team.run.completed"
    worker_messages = [
        data for event_type, data in events
        if event_type == "team.agent.message" and data["agent_name"] == "worker-1"
    ]
    assert [item["direction"] for item in worker_messages] == ["inbound", "outbound"]
    assert worker_messages[-1]["content"] == "inspection complete"


def test_team_sub_agent_emits_streamed_dialogue_deltas(tmp_path):
    class StreamingWorkerClient:
        def chat(self, _messages, tools=None, temperature=0.2, on_delta=None):
            assert tools is not None
            assert on_delta is not None
            on_delta("streamed ")
            on_delta("worker result")
            return {"role": "assistant", "content": "streamed worker result"}

    events: list[tuple[str, dict[str, Any]]] = []
    worker = SubAgent(
        "worker-1",
        AgentRole.WORKER,
        StreamingWorkerClient(),
        build_default_registry(tmp_path),
        event_callback=lambda event_type, data: events.append((event_type, data)),
    )

    result = worker.execute(
        AgentMessage.task("lead", "stream a result"),
        team_task_id="task_1",
    )

    assert result.content == "streamed worker result"
    deltas = [data for event_type, data in events if event_type == "team.agent.delta"]
    assert "".join(str(data["text"]) for data in deltas) == "streamed worker result"
    assert all(data["team_task_id"] == "task_1" for data in deltas)


def test_review_parser_is_conservative(tmp_path):
    orchestrator = AgentOrchestrator(
        DispatchClient(lambda _messages, _tools: "unused"),
        build_default_registry(tmp_path),
    )

    assert orchestrator.parse_review_approval('{"approved": true}')
    assert not orchestrator.parse_review_approval('{"approved": false}')
    assert not orchestrator.parse_review_approval('{"summary": "missing field"}')
    assert not orchestrator.parse_review_approval("unclear")
    assert not orchestrator.parse_review_approval("结果未通过审查")
    assert orchestrator.parse_review_approval("审查通过，结果合格")
    assert "problem one" in orchestrator.parse_review_issues(
        '{"approved": false, "issues": ["problem one"]}'
    )
