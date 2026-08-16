from __future__ import annotations

import json
import threading
import time
from typing import Any

import pytest

from stellarcode.agent import Agent
from stellarcode.cancellation import TaskCancelledError
from stellarcode.llm.message_history import INTERRUPTED_TOOL_RESULT
from stellarcode.tools import ToolDefinition, ToolOutput, ToolRegistry, build_default_registry


class FakeClient:
    def __init__(self) -> None:
        self.calls = 0

    def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        temperature: float = 0.2,
    ) -> dict[str, Any]:
        self.calls += 1
        if self.calls == 1:
            return {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {
                            "name": "read_file",
                            "arguments": json.dumps({"path": "todo.txt"}),
                        },
                    }
                ],
            }
        return {"role": "assistant", "content": "The todo says: ship chapter one."}


def test_agent_stops_waiting_for_a_blocking_llm_request_when_cancelled(tmp_path):
    cancellation_event = threading.Event()

    class BlockingClient:
        def chat(self, messages, tools=None, temperature=0.2):
            time.sleep(5)
            return {"role": "assistant", "content": "too late"}

    threading.Timer(0.05, cancellation_event.set).start()
    started = time.monotonic()

    with pytest.raises(TaskCancelledError, match="cancelled by user"):
        Agent(BlockingClient(), build_default_registry(tmp_path)).run(
            "wait",
            cancellation_event,
        )

    assert time.monotonic() - started < 1


def test_agent_executes_tool_call_and_returns_final_answer(tmp_path):
    (tmp_path / "todo.txt").write_text("ship chapter one", encoding="utf-8")
    registry = build_default_registry(tmp_path)
    client = FakeClient()
    agent = Agent(client, registry)

    answer = agent.run("read the todo")

    assert answer == "The todo says: ship chapter one."
    assert client.calls == 2
    assert agent.messages[-2]["role"] == "tool"
    assert agent.messages[-2]["content"] == "ship chapter one"
    assert "Current local date:" in agent.messages[0]["content"]


def test_agent_resume_repairs_an_inflight_tool_and_requires_state_verification(tmp_path):
    checkpoint_stages: list[str] = []

    class RecoveryClient:
        def chat(self, messages, tools=None, temperature=0.2):
            assert any(
                message.get("role") == "tool"
                and message.get("content") == INTERRUPTED_TOOL_RESULT
                for message in messages
            )
            assert "inspect the current workspace or system state" in messages[-1]["content"]
            return {"role": "assistant", "content": "Verified state and continued safely."}

    agent = Agent(
        RecoveryClient(),
        build_default_registry(tmp_path),
        checkpoint_callback=checkpoint_stages.append,
    )
    agent.messages.append(
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call-interrupted",
                    "type": "function",
                    "function": {
                        "name": "write_file",
                        "arguments": '{"path":"demo.txt","content":"value"}',
                    },
                }
            ],
        }
    )

    answer = agent.resume("write demo.txt")

    assert answer == "Verified state and continued safely."
    assert checkpoint_stages == ["recovery_ready", "assistant_message"]


def test_agent_reports_tool_calls_and_results_through_progress_callback(tmp_path):
    (tmp_path / "todo.txt").write_text("ship chapter one", encoding="utf-8")
    progress: list[str] = []
    agent = Agent(
        FakeClient(),
        build_default_registry(tmp_path),
        progress_callback=progress.append,
    )

    agent.run("read the todo")

    assert any(
        '[Agent 1/8] calling read_file {"path":"todo.txt"}' in message
        for message in progress
    )
    assert any("[Tool read_file] completed" in message for message in progress)


def test_agent_forces_final_answer_after_tool_iteration_limit(tmp_path):
    (tmp_path / "facts.txt").write_text("The answer is 42.", encoding="utf-8")
    progress: list[str] = []

    class LoopingClient:
        def __init__(self) -> None:
            self.calls = 0

        def chat(self, messages, tools=None, temperature=0.2):
            self.calls += 1
            if tools is None:
                assert "Do not call any more tools" in messages[-1]["content"]
                return {
                    "role": "assistant",
                    "content": "The answer is 42.",
                }
            return {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": f"read_{self.calls}",
                        "type": "function",
                        "function": {
                            "name": "read_file",
                            "arguments": '{"path":"facts.txt"}',
                        },
                    }
                ],
            }

    client = LoopingClient()
    agent = Agent(
        client,
        build_default_registry(tmp_path),
        max_iterations=2,
        progress_callback=progress.append,
    )

    answer = agent.run("find the answer")

    assert answer == "The answer is 42."
    assert client.calls == 3
    assert any("tools disabled" in message for message in progress)


def test_agent_iteration_limit_fallback_contains_last_tool_diagnostics(tmp_path):
    (tmp_path / "facts.txt").write_text("available evidence", encoding="utf-8")

    class EmptyFinalClient:
        def chat(self, messages, tools=None, temperature=0.2):
            if tools is None:
                return {"role": "assistant", "content": ""}
            return {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "read_facts",
                        "type": "function",
                        "function": {
                            "name": "read_file",
                            "arguments": '{"path":"facts.txt"}',
                        },
                    }
                ],
            }

    answer = Agent(
        EmptyFinalClient(),
        build_default_registry(tmp_path),
        max_iterations=1,
    ).run("find the answer")

    assert "Agent could not finish after 1 tool-call rounds." in answer
    assert 'Last tool: read_file {"path":"facts.txt"}' in answer
    assert "Last tool status: succeeded" in answer
    assert "available evidence" in answer


def test_agent_returns_llm_request_exception_details(tmp_path):
    class FailingClient:
        def chat(self, messages, tools=None, temperature=0.2):
            raise RuntimeError("upstream connection reset")

    progress: list[str] = []
    answer = Agent(
        FailingClient(),
        build_default_registry(tmp_path),
        progress_callback=progress.append,
    ).run("hello")

    assert "LLM request failed on iteration 1/8" in answer
    assert "RuntimeError: upstream connection reset" in answer
    assert progress and "upstream connection reset" in progress[0]


def test_agent_can_delete_a_file_with_a_dedicated_tool(tmp_path):
    class DeleteClient:
        def __init__(self) -> None:
            self.calls = 0

        def chat(self, messages, tools=None, temperature=0.2):
            self.calls += 1
            if self.calls == 1:
                tool_names = {tool["function"]["name"] for tool in tools or []}
                assert "delete_file" in tool_names
                return {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {
                            "id": "call_delete",
                            "type": "function",
                            "function": {
                                "name": "delete_file",
                                "arguments": json.dumps({"path": "obsolete.txt"}),
                            },
                        }
                    ],
                }
            assert messages[-1]["role"] == "tool"
            assert "Deleted file" in messages[-1]["content"]
            return {"role": "assistant", "content": "The file was deleted."}

    target = tmp_path / "obsolete.txt"
    target.write_text("old", encoding="utf-8")
    agent = Agent(DeleteClient(), build_default_registry(tmp_path))

    answer = agent.run("delete obsolete.txt")

    assert answer == "The file was deleted."
    assert not target.exists()


def test_agent_can_delete_a_file_outside_the_working_directory(tmp_path):
    class ExternalDeleteClient:
        def __init__(self, outside_path: str) -> None:
            self.calls = 0
            self.outside_path = outside_path

        def chat(self, messages, tools=None, temperature=0.2):
            self.calls += 1
            if self.calls == 1:
                return {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {
                            "id": "call_external_delete",
                            "type": "function",
                            "function": {
                                "name": "delete_file",
                                "arguments": json.dumps({"path": self.outside_path}),
                            },
                        }
                    ],
                }
            assert messages[-1]["role"] == "tool"
            assert "Deleted file" in messages[-1]["content"]
            return {
                "role": "assistant",
                "content": "The external file was deleted.",
            }

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside_path = tmp_path / "outside.txt"
    outside_path.write_text("remove", encoding="utf-8")
    client = ExternalDeleteClient(str(outside_path))
    agent = Agent(client, build_default_registry(workspace), max_iterations=8)

    answer = agent.run("delete the outside file")

    assert client.calls == 2
    assert answer == "The external file was deleted."
    assert not outside_path.exists()


def test_agent_limits_different_web_search_queries_per_task():
    search_queries: list[str] = []
    registry = ToolRegistry()
    registry.register(
        ToolDefinition(
            name="web_search",
            description="test search",
            parameters={"type": "object"},
            handler=lambda query: search_queries.append(query) or (
                "Search provider: stub\n"
                f"Query: {query}\n"
                "Results: 1\n"
                "Search quality: high (1/1 relevant results)"
            ),
        )
    )

    class SearchLoopClient:
        def __init__(self) -> None:
            self.calls = 0

        def chat(self, messages, tools=None, temperature=0.2):
            self.calls += 1
            if self.calls <= 5:
                return {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {
                            "id": f"search_{self.calls}",
                            "type": "function",
                            "function": {
                                "name": "web_search",
                                "arguments": json.dumps(
                                    {"query": f"different query {self.calls}"}
                                ),
                            },
                        }
                    ],
                }
            return {"role": "assistant", "content": "Finished from existing evidence."}

    progress: list[str] = []
    agent = Agent(
        SearchLoopClient(),
        registry,
        max_iterations=6,
        max_web_search_calls=4,
        progress_callback=progress.append,
    )

    answer = agent.run("research this fact")

    assert answer == "Finished from existing evidence."
    assert search_queries == [
        "different query 1",
        "different query 2",
        "different query 3",
        "different query 4",
    ]
    assert agent.messages[-2]["content"].startswith("[WEB_POLICY]")
    assert any("stub, 1, high" in message for message in progress)


def test_agent_emits_coalesced_assistant_deltas_before_final_answer():
    class StreamingClient:
        def chat(self, messages, tools=None, temperature=0.2, on_delta=None):
            assert on_delta is not None
            on_delta("hello ")
            on_delta("world")
            return {"role": "assistant", "content": "hello world"}

    events: list[tuple[str, dict[str, Any]]] = []
    agent = Agent(
        StreamingClient(),
        ToolRegistry(),
        event_callback=lambda event_type, data: events.append((event_type, data)),
    )

    answer = agent.run("say hello")

    assert answer == "hello world"
    assert "".join(
        data["text"] for event_type, data in events if event_type == "assistant.delta"
    ) == "hello world"


def test_agent_resets_provisional_stream_when_model_requests_a_tool():
    class StreamingToolClient:
        def __init__(self):
            self.calls = 0

        def chat(self, messages, tools=None, temperature=0.2, on_delta=None):
            self.calls += 1
            assert on_delta is not None
            if self.calls == 1:
                on_delta("I will inspect it first.")
                return {
                    "role": "assistant",
                    "content": "I will inspect it first.",
                    "tool_calls": [
                        {
                            "id": "call-1",
                            "type": "function",
                            "function": {"name": "echo", "arguments": "{}"},
                        }
                    ],
                }
            on_delta("Final answer")
            return {"role": "assistant", "content": "Final answer"}

    registry = ToolRegistry()
    registry.register(
        ToolDefinition(
            name="echo",
            description="echo",
            parameters={"type": "object", "properties": {}},
            handler=lambda: ToolOutput("ok"),
        )
    )
    events: list[tuple[str, dict[str, Any]]] = []
    agent = Agent(
        StreamingToolClient(),
        registry,
        event_callback=lambda event_type, data: events.append((event_type, data)),
    )

    assert agent.run("inspect") == "Final answer"
    deltas = [data for event_type, data in events if event_type == "assistant.delta"]
    assert deltas == [
        {"text": "I will inspect it first."},
        {"text": "", "reset": True},
        {"text": "Final answer"},
    ]
