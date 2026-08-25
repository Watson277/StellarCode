from __future__ import annotations

import time
from types import SimpleNamespace

from stellarcode.runtime.protocol import PROTOCOL_VERSION
from stellarcode.runtime.core import _desktop_diagnostics_snapshot
from stellarcode.runtime.sidecar import SidecarServer


def _request(method: str, params: dict, index: int = 0) -> dict:
    return {
        "kind": "request",
        "protocol_version": PROTOCOL_VERSION,
        "request_id": f"request-{method}-{index}",
        "method": method,
        "params": params,
    }


def test_desktop_diagnostics_normalizes_provider_kinds():
    snapshot = _desktop_diagnostics_snapshot(
        {
            "providers": [
                {"id": "ruff", "name": "Ruff", "kind": "linter", "available": True},
                {"id": "tsc", "name": "TypeScript", "kind": "compiler", "available": True},
                {
                    "id": "lsp",
                    "name": "Language Server",
                    "kind": "language-server",
                    "available": True,
                },
            ],
            "diagnostics": [
                {
                    "id": "note-1",
                    "severity": "info",
                    "message": "Compiler note",
                    "path": "sample.py",
                    "relative_path": "sample.py",
                    "line": 1,
                    "column": 1,
                    "source": "cargo",
                }
            ],
        }
    )

    assert {provider["id"]: provider["kind"] for provider in snapshot["providers"]} == {
        "ruff": "lint",
        "tsc": "build",
        "lsp": "lsp",
    }
    assert snapshot["problems"][0]["severity"] == "information"


def test_sidecar_routes_memory_skill_and_browser_management(tmp_path):
    messages: list[dict] = []
    calls: list[tuple] = []
    memory = {"scope": "project", "entries": [], "count": 0, "token_count": 0}
    skills = {"skills": [], "total_count": 0, "enabled_count": 0}
    browser = {
        "mode": "isolated",
        "browser_url": "",
        "last_navigated_url": "",
        "agent_opened_pages": [],
        "chrome_server": {"status": "ready", "error": "", "tool_count": 4},
    }
    runtime = SimpleNamespace(
        trace_recorder=SimpleNamespace(record=lambda *_args, **_kwargs: None),
        memory_snapshot=lambda **kwargs: calls.append(("memory.list", kwargs)) or memory,
        save_memory=lambda content: calls.append(("memory.save", content)) or memory,
        delete_memory=lambda entry_id: calls.append(("memory.delete", entry_id)) or memory,
        clear_memory=lambda: calls.append(("memory.clear",)) or memory,
        prompt_snapshot=lambda session_id, include_sensitive=False: (
            calls.append(("prompt.snapshot", session_id, include_sensitive))
            or {"available": True, "memory_hidden": not include_sensitive}
        ),
        skill_snapshot=lambda: calls.append(("skill.list",)) or skills,
        skill_detail=lambda name: {"name": name, "body": "detail"},
        skill_diff=lambda name, max_chars: (
            calls.append(("skill.diff", name, max_chars))
            or {
                "name": name,
                "diff": "--- current/SKILL.md\n+++ bundled/SKILL.md",
                "current_hash": "a" * 64,
                "builtin_hash": "b" * 64,
            }
        ),
        update_bundled_skill=lambda name, **kwargs: (
            calls.append(("skill.change", name, kwargs)) or skills
        ),
        set_skill_enabled=lambda name, enabled: (
            calls.append(("skill.enabled", name, enabled)) or skills
        ),
        reload_skills=lambda: calls.append(("skill.reload",)) or skills,
        browser_snapshot=lambda: calls.append(("browser.snapshot",)) or browser,
        probe_browser=lambda port: {"port": port, "connected": False, "error": "offline"},
        connect_browser=lambda port: {"message": "connected", "snapshot": browser},
        disconnect_browser=lambda: {"message": "disconnected", "snapshot": browser},
        browser_tabs=lambda: {"output": "tab list"},
    )
    server = SidecarServer(tmp_path, messages.append)
    server.runtime = runtime

    requests = [
        ("memory.list", {"query": "stable", "limit": 10}),
        ("memory.save", {"content": "stable fact"}),
        ("memory.delete", {"id": "mem-one"}),
        ("memory.clear", {"confirmed": True}),
        ("prompt.snapshot", {"session_id": "session-one", "include_memory": False}),
        ("skill.list", {}),
        ("skill.get", {"name": "review"}),
        ("skill.diff", {"name": "review", "max_chars": 4000}),
        ("skill.set_enabled", {"name": "review", "enabled": False}),
        ("skill.reload", {}),
        (
            "skill.update",
            {
                "name": "review",
                "current_hash": "a" * 64,
                "builtin_hash": "b" * 64,
                "confirmed": True,
            },
        ),
        (
            "skill.keep_custom",
            {
                "name": "review",
                "current_hash": "a" * 64,
                "builtin_hash": "b" * 64,
            },
        ),
        (
            "skill.restore_default",
            {
                "name": "review",
                "current_hash": "a" * 64,
                "builtin_hash": "b" * 64,
                "confirmed": True,
            },
        ),
        ("browser.snapshot", {}),
        ("browser.probe", {"port": 9222}),
        ("browser.connect", {"confirmed": True}),
        ("browser.tabs", {}),
        ("browser.disconnect", {"confirmed": True}),
    ]
    for index, (method, params) in enumerate(requests):
        assert server.handle(_request(method, params, index))

    assert all(message["ok"] for message in messages)
    assert ("memory.list", {"query": "stable", "limit": 10}) in calls
    assert ("prompt.snapshot", "session-one", False) in calls
    assert ("skill.enabled", "review", False) in calls
    assert ("skill.diff", "review", 4000) in calls
    assert (
        "skill.change",
        "review",
        {
            "action": "update",
            "expected_current_hash": "a" * 64,
            "expected_builtin_hash": "b" * 64,
        },
    ) in calls
    assert messages[-2]["result"] == {"output": "tab list"}


def test_sidecar_requires_confirmation_for_destructive_management(tmp_path):
    messages: list[dict] = []
    runtime = SimpleNamespace(
        trace_recorder=SimpleNamespace(record=lambda *_args, **_kwargs: None),
        clear_memory=lambda: {},
        connect_browser=lambda _port: {},
        disconnect_browser=lambda: {},
    )
    server = SidecarServer(tmp_path, messages.append)
    server.runtime = runtime

    for index, (method, params) in enumerate(
        [
            ("memory.clear", {}),
            ("browser.connect", {}),
            ("browser.disconnect", {}),
            (
                "skill.update",
                {
                    "name": "review",
                    "current_hash": "a" * 64,
                    "builtin_hash": "b" * 64,
                },
            ),
            (
                "skill.restore_default",
                {
                    "name": "review",
                    "current_hash": "a" * 64,
                    "builtin_hash": "b" * 64,
                },
            ),
            ("diagnostics.run", {"profile": "build"}),
        ]
    ):
        server.handle(_request(method, params, index))

    assert all(message["ok"] is False for message in messages)
    assert all("confirmed=true" in message["error"]["message"] for message in messages)


def test_sidecar_streams_and_cancels_diagnostics(tmp_path):
    messages: list[dict] = []
    initial = {
        "workspace": str(tmp_path),
        "status": "not_run",
        "run_id": None,
        "problems": [],
        "error_count": 0,
        "warning_count": 0,
        "information_count": 0,
        "providers": [],
        "detected_projects": [],
        "stale": False,
    }

    received_run_ids: list[str] = []

    def run(profile, progress, cancel_event, *, run_id):
        received_run_ids.append(run_id)
        progress("checking")
        while not cancel_event.is_set():
            time.sleep(0.005)
        return {**initial, "status": "cancelled", "profile": profile}

    runtime = SimpleNamespace(
        trace_recorder=SimpleNamespace(record=lambda *_args, **_kwargs: None),
        diagnostics_snapshot=lambda: initial,
        run_diagnostics=run,
        record_runtime_event=lambda *_args, **_kwargs: None,
    )
    server = SidecarServer(tmp_path, messages.append)
    server.runtime = runtime
    server.project_id = "project-diagnostics"

    server.handle(_request("diagnostics.run", {"profile": "safe"}))
    run_id = messages[0]["result"]["run_id"]
    deadline = time.monotonic() + 1
    while not any(message.get("type") == "diagnostics.progress" for message in messages):
        assert time.monotonic() < deadline
        time.sleep(0.005)
    server.handle(_request("diagnostics.cancel", {"run_id": run_id}, 1))
    while server.active_diagnostics_job_id and time.monotonic() < deadline:
        time.sleep(0.005)

    event_types = [message.get("type") for message in messages if message.get("kind") == "event"]
    assert event_types == [
        "diagnostics.started",
        "diagnostics.progress",
        "diagnostics.cancelled",
    ]
    cancelled = next(message for message in messages if message.get("type") == "diagnostics.cancelled")
    assert cancelled["data"] == {"run_id": run_id}
    assert received_run_ids == [run_id]
    assert server.active_diagnostics_job_id is None
