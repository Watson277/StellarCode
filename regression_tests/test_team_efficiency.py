"""Offline Team efficiency regressions. These tests never call a remote model."""

import json
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from stellarcode.llm.team_budget import team_request_options
from stellarcode.llm.types import llm_operation
from stellarcode.llm.openai_stream import consume_chat_completion_stream
from stellarcode.llm.compatible_client import OpenAICompatibleClient, CompatibleApiError
from stellarcode.multi_agent import AgentOrchestrator
from stellarcode.multi_agent.orchestrator import StepExecutionResult, MultiAgentError
from stellarcode.plan import ExecutionPlan, Task, TaskType, Planner, PlanValidationError
from stellarcode.tools import build_default_registry


@pytest.fixture(autouse=True)
def clean_team_env(monkeypatch):
    import os
    for key in list(os.environ):
        if key.startswith("TEAM_"):
            monkeypatch.delenv(key)


def test_parallel_role_budgets_are_isolated():
    barrier = threading.Barrier(3)

    def options(role):
        with llm_operation("team-" + role):
            barrier.wait(timeout=3)
            return team_request_options()

    with ThreadPoolExecutor(max_workers=3) as pool:
        result = list(pool.map(options, ["planner", "worker", "reviewer"]))
    assert [item["max_tokens"] for item in result] == [4096, 8192, 4096]
    assert all(item["reasoning_effort"] == "low" for item in result)
    assert team_request_options() == {}


def test_optional_api_parameters_and_role_overrides(monkeypatch):
    monkeypatch.setenv("TEAM_REASONING_EFFORT", "")
    monkeypatch.setenv("TEAM_WORKER_REASONING_EFFORT", "high")
    monkeypatch.setenv("TEAM_OUTPUT_TOKEN_PARAMETER", "max_completion_tokens")
    monkeypatch.setenv("TEAM_WORKER_MAX_OUTPUT_TOKENS", "1234")
    with llm_operation("team-worker"):
        assert team_request_options() == {"reasoning_effort": "high", "max_completion_tokens": 1234}
    monkeypatch.setenv("TEAM_REVIEWER_MAX_OUTPUT_TOKENS", "0")
    with llm_operation("team-reviewer"):
        assert team_request_options() == {}


def test_stream_length_limit_never_returns_partial_tool_call():
    lines = [
        'data: ' + json.dumps({"choices": [{"delta": {"tool_calls": [
            {"index": 0, "id": "x", "function": {"name": "write_file", "arguments": '{"path":'}}
        ]}}]}),
        'data: ' + json.dumps({"choices": [{"finish_reason": "length", "delta": {}}]}),
        'data: [DONE]',
    ]
    with pytest.raises(RuntimeError, match="output limit"):
        consume_chat_completion_stream(lines)


def test_handoff_preserves_transitive_contracts_and_tool_evidence(tmp_path):
    agent = AgentOrchestrator(object(), build_default_registry(tmp_path), workspace=tmp_path)
    plan = ExecutionPlan(id="p", goal="goal")
    for name, deps in [("a", []), ("b", ["a"]), ("c", ["b"])]:
        plan.add_task(Task(name, name, TaskType.ANALYSIS, dependencies=deps))
    plan.compute_execution_order()
    report = json.dumps({"summary": "x" * 600, "interfaces": ["create_app(storage)"]})
    plan.get_task("a").mark_completed(report)
    plan.get_task("b").mark_completed("model ready")
    plan.get_task("c").contract = {"write_paths": ["README.md"], "non_goals": ["install dependencies"]}
    agent.last_step_results["a"] = StepExecutionResult(
        "a", "worker-1", True, result=report,
        tool_evidence=({"tool": "execute_command", "result": "exit_code: 0; Ran 5 tests"},),
    )
    context = agent._build_step_context(plan, plan.get_task("c"))
    assert "create_app(storage)" in context
    assert "Recorded tool evidence" in context and "Ran 5 tests" in context
    assert "README.md" in context and "install dependencies" in context


def test_planner_contract_is_validated_and_preserved(tmp_path):
    parser = Planner(object(), tmp_path)
    step = {"id": "a", "description": "docs", "write_paths": ["README.md"], "non_goals": ["install"]}
    plan = parser.parse_plan("goal", json.dumps({"steps": [step]}))
    assert plan.get_task("task_1").contract["write_paths"] == ["README.md"]
    step["write_paths"] = "README.md"
    with pytest.raises(PlanValidationError, match="string list"):
        parser.parse_plan("goal", json.dumps({"steps": [step]}))


def test_blocked_handoff_is_not_reviewed_or_marked_completed(tmp_path):
    class Client:
        def chat(self, messages, tools=None, temperature=0.2):
            if "planner in a multi-agent" in messages[0]["content"]:
                return {"content": json.dumps({"steps": [{"id": "a", "description": "build"}]})}
            assert "worker in a multi-agent" in messages[0]["content"]
            return {"content": '{"status":"blocked","remaining_issues":["missing Python"]}'}

    agent = AgentOrchestrator(Client(), build_default_registry(tmp_path), workspace=tmp_path)
    with pytest.raises(MultiAgentError, match="missing Python"):
        agent.run("build")


@pytest.mark.parametrize("streaming", [False, True])
def test_role_budget_reaches_actual_http_payload(monkeypatch, streaming):
    import httpx
    real_client = httpx.Client
    payloads = []

    def respond(request):
        payloads.append(json.loads(request.content))
        if streaming:
            return httpx.Response(200, text=(
                'data: {"choices":[{"delta":{"content":"ok"},"finish_reason":"stop"}]}\n\n'
                'data: [DONE]\n\n'
            ))
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    monkeypatch.setattr(httpx, "Client", lambda **kw: real_client(
        transport=httpx.MockTransport(respond), **kw,
    ))
    client = OpenAICompatibleClient(model="configured-model", base_url="https://test.invalid/v1")
    with llm_operation("team-worker"):
        client.chat([{"role": "user", "content": "work"}],
                    on_delta=(lambda text: None) if streaming else None)
    assert payloads[0]["reasoning_effort"] == "low"
    assert payloads[0]["max_tokens"] == 8192


def test_unsupported_reasoning_is_not_silently_removed(monkeypatch):
    import httpx
    real_client = httpx.Client
    payloads = []

    def respond(request):
        payloads.append(json.loads(request.content))
        return httpx.Response(400, json={"error": {"message": "reasoning_effort unsupported"}})

    monkeypatch.setattr(httpx, "Client", lambda **kw: real_client(
        transport=httpx.MockTransport(respond), **kw,
    ))
    client = OpenAICompatibleClient(model="custom", base_url="https://test.invalid/v1")
    with llm_operation("team-worker"), pytest.raises(CompatibleApiError):
        client.chat([{"role": "user", "content": "work"}], on_delta=lambda _: None)
    assert payloads and all(p["reasoning_effort"] == "low" for p in payloads)


def test_nonstream_length_limit_is_not_a_success(monkeypatch):
    import httpx
    real_client = httpx.Client
    monkeypatch.setattr(httpx, "Client", lambda **kw: real_client(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json={"choices": [{
            "message": {"content": "unfinished"}, "finish_reason": "length",
        }]})), **kw,
    ))
    client = OpenAICompatibleClient(model="custom", base_url="https://test.invalid/v1")
    with pytest.raises(RuntimeError, match="output limit"):
        client.chat([{"role": "user", "content": "work"}])
