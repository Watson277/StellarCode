from __future__ import annotations

import json
import os
import sys
import threading
import time
from typing import Any

from stellarcode.agent import Agent
from stellarcode.hitl import TerminalHitlHandler
from stellarcode.hitl.registry import HitlToolRegistry
from stellarcode.llm.types import llm_runtime_scope
from stellarcode.runtime.hitl import RuntimeHitlHandler
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


def test_command_timeout_terminates_descendant_process_tree(tmp_path):
    registry = build_default_registry(tmp_path)
    child_code = "import time; time.sleep(30)"
    parent_code = (
        "import subprocess, sys, time; "
        f"subprocess.Popen([sys.executable, '-c', {child_code!r}]); "
        "print('parent started', flush=True); time.sleep(30)"
    )

    started = time.monotonic()
    result = registry.execute_tools(
        [
            ToolInvocation(
                "command-timeout",
                "execute_command",
                {
                    "command": [sys.executable, "-c", parent_code],
                    "timeout_seconds": 0.2,
                },
            )
        ]
    )[0]
    elapsed = time.monotonic() - started

    assert elapsed < (8 if os.name == "nt" else 3)
    assert not result.success
    assert "Command timed out after 0.2s" in result.result
    assert "parent started" in result.result


def test_batch_timeout_cancels_running_command_process(tmp_path):
    registry = build_default_registry(
        tmp_path,
        max_parallel_tools=2,
        tool_batch_timeout_seconds=0.2,
    )
    started = time.monotonic()
    results = registry.execute_tools(
        [
            ToolInvocation(
                "slow-command",
                "execute_command",
                {
                    "command": [sys.executable, "-c", "import time; time.sleep(30)"],
                    "timeout_seconds": 30,
                },
            ),
            ToolInvocation("quick-list", "list_dir", {"path": "."}),
        ]
    )
    elapsed = time.monotonic() - started

    assert elapsed < 2
    assert results[0].timed_out
    assert "batch timeout" in results[0].result
    assert results[1].success


def test_task_cancellation_stops_running_command_process(tmp_path):
    registry = build_default_registry(tmp_path)
    cancellation_event = threading.Event()
    threading.Timer(0.2, cancellation_event.set).start()

    started = time.monotonic()
    result = registry.execute_tools(
        [
            ToolInvocation(
                "cancel-command",
                "execute_command",
                {
                    "command": [sys.executable, "-c", "import time; time.sleep(30)"],
                    "timeout_seconds": 30,
                },
            )
        ],
        cancellation_event=cancellation_event,
    )[0]

    assert time.monotonic() - started < (8 if os.name == "nt" else 3)
    assert not result.success
    assert "cancelled" in result.result.lower()


def test_task_cancellation_stops_waiting_for_a_blocking_generic_tool():
    registry = _sleep_registry()
    cancellation_event = threading.Event()
    threading.Timer(0.05, cancellation_event.set).start()

    started = time.monotonic()
    result = registry.execute_tools(
        [ToolInvocation("slow", "sleep_tool", {"label": "late", "delay": 5})],
        cancellation_event=cancellation_event,
    )[0]

    assert time.monotonic() - started < 1
    assert not result.success
    assert "cancelled" in result.result.lower()


def test_quiescence_waits_for_a_detached_cancelled_tool_to_finish():
    registry = _sleep_registry()
    cancellation_event = threading.Event()
    threading.Timer(0.02, cancellation_event.set).start()

    result = registry.execute_tools(
        [ToolInvocation("slow", "sleep_tool", {"label": "late", "delay": 0.2})],
        cancellation_event=cancellation_event,
    )[0]

    assert not result.success
    started = time.monotonic()
    assert registry.wait_for_quiescence(timeout_seconds=1)
    assert time.monotonic() - started >= 0.1


def test_quiescence_is_scoped_to_the_owning_task():
    registry = ToolRegistry()
    started = threading.Barrier(3)
    releases = {"one": threading.Event(), "two": threading.Event()}

    def blocking(label: str) -> str:
        started.wait()
        releases[label].wait()
        return label

    registry.register(
        ToolDefinition("blocking", "Test helper.", {"type": "object"}, blocking)
    )

    def run(task_id: str, label: str) -> None:
        with llm_runtime_scope(f"session-{label}", task_id):
            registry.execute("blocking", {"label": label})

    first = threading.Thread(target=run, args=("task-one", "one"), daemon=True)
    second = threading.Thread(target=run, args=("task-two", "two"), daemon=True)
    first.start()
    second.start()
    started.wait()
    releases["one"].set()
    first.join(timeout=1)

    assert registry.wait_for_quiescence(timeout_seconds=0.1, task_id="task-one")
    assert not registry.wait_for_quiescence(timeout_seconds=0.05)
    assert second.is_alive()
    releases["two"].set()
    second.join(timeout=1)
    assert registry.wait_for_quiescence(timeout_seconds=0.1)


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


def test_desktop_batch_publishes_all_approvals_before_waiting(tmp_path):
    condition = threading.Condition()
    requested: list[dict[str, Any]] = []
    results: list[Any] = []

    def emit(event_type: str, data: dict[str, Any]) -> None:
        if event_type != "approval.requested":
            return
        with condition:
            requested.append(data)
            condition.notify_all()

    handler = RuntimeHitlHandler(emit)
    registry = build_default_registry(tmp_path, hitl_handler=handler)

    def execute_batch() -> None:
        with llm_runtime_scope("session-one", "task-one"):
            results.extend(registry.execute_tools([
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
            ]))

    worker = threading.Thread(target=execute_batch, daemon=True)
    worker.start()
    with condition:
        assert condition.wait_for(lambda: len(requested) == 2, timeout=2)

    contexts = [handler.context(item["approval_id"]) for item in requested]
    assert contexts == [("session-one", "task-one"), ("session-one", "task-one")]
    for item in requested:
        assert handler.resolve(item["approval_id"], "approve")

    worker.join(timeout=2)
    assert not worker.is_alive()
    assert all(result.success for result in results)
    assert (tmp_path / "one.txt").read_text(encoding="utf-8") == "one"
    assert (tmp_path / "two.txt").read_text(encoding="utf-8") == "two"


def test_stop_releases_remaining_approvals_after_one_batch_rejection(tmp_path):
    condition = threading.Condition()
    requested: list[dict[str, Any]] = []
    results: list[Any] = []
    cancellation_event = threading.Event()

    def emit(event_type: str, data: dict[str, Any]) -> None:
        if event_type != "approval.requested":
            return
        with condition:
            requested.append(data)
            condition.notify_all()

    handler = RuntimeHitlHandler(emit)
    registry = build_default_registry(tmp_path, hitl_handler=handler)

    def execute_batch() -> None:
        with llm_runtime_scope("session-one", "task-one"):
            results.extend(registry.execute_tools(
                [
                    ToolInvocation(
                        "one",
                        "execute_command",
                        {"command": "echo one"},
                    ),
                    ToolInvocation(
                        "two",
                        "execute_command",
                        {"command": "echo two"},
                    ),
                ],
                cancellation_event=cancellation_event,
            ))

    worker = threading.Thread(target=execute_batch, daemon=True)
    worker.start()
    with condition:
        assert condition.wait_for(lambda: len(requested) == 2, timeout=2)

    assert handler.resolve(requested[0]["approval_id"], "reject")
    cancellation_event.set()
    handler.reject_task("task-one", "Task cancelled by user.")

    worker.join(timeout=2)
    assert not worker.is_alive()
    assert len(results) == 2
    assert all(not result.success for result in results)


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
