from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from stellarcode.browser import BrowserMode, BrowserProbe, BrowserSession
from stellarcode.mcp import McpServerStatus
from stellarcode.memory import ProjectMemoryService
from stellarcode.runtime.core import RuntimeSession
from stellarcode.skill import SkillRegistry, SkillStateStore


def _write_skill(root: Path, name: str, body: str = "Use this guidance.") -> None:
    directory = root / name
    directory.mkdir(parents=True)
    (directory / "SKILL.md").write_text(
        "---\n"
        f"name: {name}\n"
        "description: Test management skill\n"
        'version: "1.0"\n'
        "author: Test\n"
        "tags: [desktop, test]\n"
        "---\n\n"
        f"{body}\n",
        encoding="utf-8",
    )


def test_project_memory_management_is_structured_and_returns_canonical_dedupe(
    tmp_path,
):
    service = ProjectMemoryService(tmp_path / "memory")

    created, was_created = service.save("The project uses Python 3.12")
    duplicate, duplicate_created = service.save("The project uses Python 3.12")
    service.save("Build with Ruff")

    snapshot = service.snapshot()
    filtered = service.snapshot("Python", limit=1)

    assert was_created is True
    assert duplicate_created is False
    assert duplicate.id == created.id
    assert snapshot["scope"] == "project"
    assert snapshot["count"] == 2
    assert snapshot["returned_count"] == 2
    assert snapshot["token_count"] > 0
    assert snapshot["storage_path"].endswith("long_term_memory.json")
    assert filtered["returned_count"] == 1
    assert filtered["entries"][0]["id"] == created.id

    assert service.delete(created.id) is True
    assert service.snapshot()["count"] == 1
    service.clear()
    assert service.snapshot()["entries"] == []


def test_runtime_skill_management_returns_details_and_persists_state(tmp_path):
    skills = tmp_path / "skills"
    _write_skill(skills, "review")
    state_store = SkillStateStore(tmp_path / "skills.json")
    registry = SkillRegistry(None, skills, state_store)
    registry.reload()
    runtime = object.__new__(RuntimeSession)
    runtime.skill_registry = registry
    runtime.skill_state_store = state_store
    runtime.skill_upgrade_manager = SimpleNamespace(
        statuses=lambda: {},
        warnings=lambda: (),
        bootstrap=lambda: (),
    )

    snapshot = runtime.skill_snapshot()
    detail = runtime.skill_detail("review")

    assert snapshot["enabled_count"] == 1
    assert snapshot["total_count"] == 1
    assert snapshot["skills"][0]["source"] == "project"
    assert snapshot["skills"][0]["enabled"] is True
    assert detail["body"].strip() == "Use this guidance."

    disabled = runtime.set_skill_enabled("review", False)
    assert disabled["enabled_count"] == 0
    assert state_store.disabled() == frozenset({"review"})

    _write_skill(skills, "format")
    reloaded = runtime.reload_skills()
    assert reloaded["total_count"] == 2



class _BrowserManager:
    def __init__(self) -> None:
        self.server_value = SimpleNamespace(
            name="chrome-devtools",
            status=McpServerStatus.READY,
            error_message="",
            tools=[object(), object()],
        )

    def server(self, name: str):
        return self.server_value if name == "chrome-devtools" else None

    @staticmethod
    def server_snapshot(server) -> dict[str, object]:
        return {
            "status": server.status.value,
            "error": server.error_message,
            "tool_count": len(server.tools),
        }


class _BrowserController:
    def __init__(self, session: BrowserSession) -> None:
        self.session = session
        self.sync_count = 0
        self.connectivity = SimpleNamespace(
            probe=lambda port: BrowserProbe(
                True,
                browser_url=f"http://127.0.0.1:{port}",
                browser_version="Chrome/Test",
            )
        )

    def connect(self, port: int | None = None) -> str:
        target = f"http://127.0.0.1:{port}" if port is not None else "autoConnect"
        self.session.switch_to_shared(target)
        return "connected"

    def disconnect(self) -> str:
        self.session.switch_to_isolated()
        return "disconnected"

    @staticmethod
    def tabs() -> str:
        return "1: https://example.com [selected]"

    def sync_from_server(self) -> None:
        self.sync_count += 1


def test_runtime_browser_management_returns_structured_state_and_syncs_mcp():
    runtime = object.__new__(RuntimeSession)
    runtime.browser_session = BrowserSession()
    runtime.browser_controller = _BrowserController(runtime.browser_session)
    runtime.mcp_manager = _BrowserManager()
    runtime.project_id = "project-one"
    emitted: list[tuple] = []
    runtime._emit = lambda *args: emitted.append(args)

    initial = runtime.browser_snapshot()
    probe = runtime.probe_browser(9333)
    connected = runtime.connect_browser()
    tabs = runtime.browser_tabs()

    assert initial == {
        "mode": "isolated",
        "browser_url": "",
        "last_navigated_url": "",
        "agent_opened_pages": [],
        "chrome_server": {"status": "ready", "error": "", "tool_count": 2},
    }
    assert probe == {
        "port": 9333,
        "connected": True,
        "browser_url": "http://127.0.0.1:9333",
        "browser_version": "Chrome/Test",
        "error": "",
    }
    assert connected["snapshot"]["mode"] == BrowserMode.SHARED.value
    assert connected["snapshot"]["browser_url"] == "autoConnect"
    assert "example.com" in tabs["output"]

    runtime._mcp_status_changed(runtime.mcp_manager.server_value)
    assert runtime.browser_controller.sync_count == 1
    assert emitted[-1][0] == "mcp.status_changed"

    disconnected = runtime.disconnect_browser()
    assert disconnected["snapshot"]["mode"] == BrowserMode.ISOLATED.value
