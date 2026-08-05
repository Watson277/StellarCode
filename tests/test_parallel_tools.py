from __future__ import annotations

import json
import threading
import time
from typing import Any

from stellarcode.agent import Agent
from stellarcode.hitl import TerminalHitlHandler
from stellarcode.hitl.registry import HitlToolRegistry
from stellarcode.tools import (
    ToolDefinition,
    ToolInvocation,
    ToolRegistry,
    build_default_registry,
)
from stellarcode.tools.command_policy import is_full_disk_recursive_scan


def _sleep_registry(
    max_parallel_tools: int = 4,
    batch_timeout_seconds: float = 1,
) -> ToolRegistry:
    registry = ToolRegistry(
        max_parallel_tools=max_parallel_tools,
        batch_timeout_seconds=batch_timeout_seconds,
    )
    registry.register(
        ToolDefinition(
            name="sleep_tool",
            description="Test helper.",
            parameters={"type": "object"},
            handler=lambda label, delay=0: _sleep_and_return(label, delay),
        )
    )
    return registry


def _sleep_and_return(label: str, delay: float) -> str:
    time.sleep(delay)
    return label


def test_parallel_tools_run_concurrently_and_keep_invocation_order():
    current = 0
    peak = 0
    lock = threading.Lock()
    registry = ToolRegistry(max_parallel_tools=4, batch_timeout_seconds=1)

    def tracked(label: str, delay: float) -> str:
        nonlocal current, peak
        with lock:
            current += 1
            peak = max(peak, current)
        time.sleep(delay)
        with lock:
            current -= 1
        return label

    registry.register(
        ToolDefinition("tracked", "Test helper.", {"type": "object"}, tracked)
    )
    invocations = [
        ToolInvocation("slow", "tracked", {"label": "first", "delay": 0.12}),
        ToolInvocation("fast", "tracked", {"label": "second", "delay": 0.02}),
        ToolInvocation("medium", "tracked", {"label": "third", "delay": 0.07}),
    ]

    started = time.monotonic()
    results = registry.execute_tools(invocations)
    elapsed = time.monotonic() - started

    assert peak == 3
    assert elapsed < 0.2
    assert [result.id for result in results] == ["slow", "fast", "medium"]
    assert [result.result for result in results] == ["first", "second", "third"]


def test_parallel_tools_respect_maximum_parallelism():
    current = 0
    peak = 0
    lock = threading.Lock()
    registry = ToolRegistry(max_parallel_tools=2, batch_timeout_seconds=2)

    def tracked(index: int) -> str:
        nonlocal current, peak
        with lock:
            current += 1
            peak = max(peak, current)
        time.sleep(0.03)
        with lock:
            current -= 1
        return str(index)

    registry.register(
        ToolDefinition("tracked", "Test helper.", {"type": "object"}, tracked)
    )
    results = registry.execute_tools(
        [ToolInvocation(str(index), "tracked", {"index": index}) for index in range(6)]
    )

    assert peak == 2
    assert [result.result for result in results] == [str(index) for index in range(6)]


def test_parallel_tool_batch_timeout_is_reported_without_waiting_for_stuck_tool():
    registry = _sleep_registry(batch_timeout_seconds=0.05)
    invocations = [
        ToolInvocation("quick", "sleep_tool", {"label": "done", "delay": 0.01}),
        ToolInvocation("slow", "sleep_tool", {"label": "late", "delay": 0.3}),
    ]

    started = time.monotonic()
    results = registry.execute_tools(invocations)
    elapsed = time.monotonic() - started

    assert elapsed < 0.2
    assert results[0].result == "done"
    assert results[0].success
    assert results[1].timed_out
    assert not results[1].success
    assert "batch timeout" in results[1].result


def test_parallel_tool_failure_does_not_discard_other_results():
    registry = ToolRegistry()

    def sometimes_fails(label: str) -> str:
        if label == "bad":
            raise RuntimeError("boom")
        return label

    registry.register(
        ToolDefinition("fragile", "Test helper.", {"type": "object"}, sometimes_fails)
    )
    results = registry.execute_tools(
        [
            ToolInvocation("ok", "fragile", {"label": "good"}),
            ToolInvocation("bad", "fragile", {"label": "bad"}),
        ]
    )

    assert results[0].result == "good"
    assert results[0].success
    assert results[1].result == "Tool error: fragile failed: boom"
    assert not results[1].success


def test_single_tool_uses_calling_thread_without_worker_creation():
    registry = ToolRegistry()
    registry.register(
        ToolDefinition(
            "thread_name",
            "Test helper.",
            {"type": "object"},
            lambda: threading.current_thread().name,
        )
    )

    result = registry.execute_tools([ToolInvocation("one", "thread_name")])[0]

    assert result.result == threading.current_thread().name
    assert not result.result.startswith("stellarcode-tool-")


def test_restricted_batch_collects_approvals_before_parallel_execution(tmp_path):
    answers = iter(["y", "y"])
    handler = TerminalHitlHandler(
        enabled=True,
        input_func=lambda _prompt: next(answers),
        output_func=lambda _message: None,
    )
    registry = build_default_registry(tmp_path, hitl_handler=handler)
    results = registry.execute_tools(
        [
            ToolInvocation(
                "one",
                "write_file",
                {"path": "one.txt", "content": "one"},
            ),
            ToolInvocation(
                "two",
                "write_file",
                {"path": "two.txt", "content": "two"},
            ),
        ]
    )

    assert all(result.success for result in results)
    assert (tmp_path / "one.txt").read_text(encoding="utf-8") == "one"
    assert (tmp_path / "two.txt").read_text(encoding="utf-8") == "two"


def test_restricted_mode_blocks_full_disk_scan_but_full_access_bypasses_policy():
    delegate = ToolRegistry()
    delegate.register(
        ToolDefinition(
            "execute_command",
            "Test helper.",
            {"type": "object"},
            lambda command: f"executed: {command}",
        )
    )
    handler = TerminalHitlHandler(
        enabled=True,
        input_func=lambda _prompt: "y",
        output_func=lambda _message: None,
    )
    registry = HitlToolRegistry(delegate, handler)
    arguments = {"command": "Get-ChildItem C:\\ -Recurse"}

    blocked = registry.execute("execute_command", arguments)
    handler.set_enabled(False)
    unrestricted = registry.execute("execute_command", arguments)

    assert blocked.startswith("[POLICY]")
    assert "Full-disk recursive scans" in blocked
    assert unrestricted.startswith("executed:")


def test_full_disk_scan_policy_recognizes_posix_and_windows_forms():
    assert is_full_disk_recursive_scan("find / -name '*.py'")
    assert is_full_disk_recursive_scan(["find", "$HOME", "-type", "f"])
    assert is_full_disk_recursive_scan("Get-ChildItem C:\\ -Recurse")
    assert is_full_disk_recursive_scan("dir C:\\ /s")
    assert not is_full_disk_recursive_scan("find ./src -name '*.py'")
    assert not is_full_disk_recursive_scan("Get-ChildItem C:\\project -Recurse")


def test_react_agent_executes_same_round_tool_calls_in_parallel():
    barrier = threading.Barrier(2)
    registry = ToolRegistry()
    registry.register(
        ToolDefinition(
            "barrier",
            "Test helper.",
            {"type": "object"},
            lambda value: _wait_at_barrier(barrier, value),
        )
    )

    class ParallelCallClient:
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
                            "id": "first",
                            "type": "function",
                            "function": {
                                "name": "barrier",
                                "arguments": json.dumps({"value": "A"}),
                            },
                        },
                        {
                            "id": "second",
                            "type": "function",
                            "function": {
                                "name": "barrier",
                                "arguments": json.dumps({"value": "B"}),
                            },
                        },
                    ],
                }
            assert [message["content"] for message in messages[-2:]] == ["A", "B"]
            return {"role": "assistant", "content": "parallel complete"}

    answer = Agent(ParallelCallClient(), registry).run("run both")

    assert answer == "parallel complete"


def _wait_at_barrier(barrier: threading.Barrier, value: str) -> str:
    barrier.wait(timeout=2)
    return value
