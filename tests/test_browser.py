from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from stellarcode.browser import (
    BrowserController,
    BrowserGuard,
    BrowserMode,
    BrowserProbe,
    register_browser_tools,
)
from stellarcode.browser.session import BrowserSession
from stellarcode.cli import handle_browser_command
from stellarcode.mcp import McpServerStatus
from stellarcode.tools import ToolDefinition, ToolExecutionError, ToolRegistry


@dataclass
class FakeConfig:
    args: list[str]


@dataclass
class FakeServer:
    config: FakeConfig
    status: McpServerStatus = McpServerStatus.READY
    tools: list[object] = field(default_factory=lambda: [object(), object()])
    error_message: str = ""


class FakeManager:
    def __init__(
        self,
        args: list[str] | None = None,
        restart_statuses: list[McpServerStatus] | None = None,
    ) -> None:
        self.chrome = FakeServer(FakeConfig(args or ["--isolated=true"]))
        self.restart_statuses = list(restart_statuses or [])
        self.restart_calls: list[list[str]] = []

    def server(self, name: str):
        return self.chrome if name == "chrome-devtools" else None

    def restart_with_args(self, _name: str, args: list[str]) -> str:
        self.restart_calls.append(list(args))
        self.chrome.config.args = list(args)
        if self.restart_statuses:
            self.chrome.status = self.restart_statuses.pop(0)
        else:
            self.chrome.status = McpServerStatus.READY
        self.chrome.error_message = (
            "connection refused"
            if self.chrome.status == McpServerStatus.ERROR
            else ""
        )
        return (
            "MCP server restarted: chrome-devtools"
            if self.chrome.status == McpServerStatus.READY
            else "MCP server restart failed: chrome-devtools"
        )


class FakeConnectivity:
    def __init__(self, probe: BrowserProbe) -> None:
        self.result = probe
        self.ports: list[int] = []

    def probe(self, port: int) -> BrowserProbe:
        self.ports.append(port)
        return self.result


def _controller(
    manager: FakeManager,
    probe: BrowserProbe | None = None,
) -> BrowserController:
    return BrowserController(
        BrowserSession(),
        manager,  # type: ignore[arg-type]
        ToolRegistry(),
        FakeConnectivity(probe or BrowserProbe(False, error="offline")),
    )


def test_browser_session_tracks_mode_navigation_and_agent_pages():
    session = BrowserSession()

    session.switch_to_shared("autoConnect")
    session.remember_navigation("https://example.com")
    session.record_opened_page(3)

    assert session.mode == BrowserMode.SHARED
    assert session.browser_url == "autoConnect"
    assert session.last_navigated_url == "https://example.com"
    assert session.is_agent_opened_page(3)

    session.switch_to_isolated()

    assert session.mode == BrowserMode.ISOLATED
    assert session.agent_opened_pages == ()


def test_browser_guard_blocks_user_tab_but_allows_agent_opened_tab():
    session = BrowserSession()
    session.switch_to_shared("autoConnect")
    guard = BrowserGuard(session)

    with pytest.raises(ToolExecutionError, match="user-owned tabs"):
        guard.before_call(
            "mcp__chrome-devtools__close_page",
            {"pageId": 1},
        )

    guard.after_call(
        "mcp__chrome-devtools__new_page",
        {"url": "https://example.com"},
        "## Pages\n1: chrome://inspect\n2: https://example.com [selected]",
    )
    guard.before_call(
        "mcp__chrome-devtools__close_page",
        {"pageId": 2},
    )

    assert session.is_agent_opened_page(2)
    assert session.last_navigated_url == "https://example.com"

    guard.after_call(
        "mcp__chrome-devtools__close_page",
        {"pageId": 2},
        "The page has been closed.",
    )
    assert not session.is_agent_opened_page(2)


def test_auto_connect_switches_runtime_args_without_persisting_config():
    manager = FakeManager()
    controller = _controller(manager)

    result = controller.connect()

    assert controller.session.mode == BrowserMode.SHARED
    assert controller.session.browser_url == "autoConnect"
    assert manager.restart_calls == [
        ["-y", "chrome-devtools-mcp@latest", "--autoConnect"]
    ]
    assert "Connected to shared Chrome" in result


def test_failed_auto_connect_rolls_back_previous_args_and_mode():
    old_args = ["-y", "chrome-devtools-mcp@latest", "--isolated=true"]
    manager = FakeManager(
        old_args,
        [McpServerStatus.ERROR, McpServerStatus.READY],
    )
    controller = _controller(manager)

    result = controller.connect()

    assert manager.restart_calls == [
        ["-y", "chrome-devtools-mcp@latest", "--autoConnect"],
        old_args,
    ]
    assert controller.session.mode == BrowserMode.ISOLATED
    assert "restored the previous MCP arguments" in result


def test_agent_browser_connect_tool_reports_failed_shared_connection():
    manager = FakeManager(
        restart_statuses=[McpServerStatus.ERROR, McpServerStatus.READY]
    )
    controller = _controller(manager)
    register_browser_tools(controller.registry, controller)

    with pytest.raises(ToolExecutionError, match="Shared Chrome connection failed"):
        controller.registry.execute("browser_connect", {})


def test_legacy_connect_probes_before_changing_mcp_args():
    manager = FakeManager()
    connectivity = FakeConnectivity(BrowserProbe(False, error="connection refused"))
    controller = BrowserController(
        BrowserSession(),
        manager,  # type: ignore[arg-type]
        ToolRegistry(),
        connectivity,
    )

    result = controller.connect(9222)

    assert connectivity.ports == [9222]
    assert manager.restart_calls == []
    assert "No Chrome CDP endpoint" in result


def test_legacy_connect_uses_browser_url_after_successful_probe():
    manager = FakeManager()
    controller = _controller(
        manager,
        BrowserProbe(True, browser_url="http://127.0.0.1:9333"),
    )

    result = controller.connect(9333)

    assert manager.restart_calls[0][-1] == (
        "--browser-url=http://127.0.0.1:9333"
    )
    assert controller.session.mode == BrowserMode.SHARED
    assert "Connected to shared Chrome" in result


def test_browser_tabs_are_only_available_in_shared_mode():
    manager = FakeManager()
    registry = ToolRegistry()
    registry.register(
        ToolDefinition(
            name="mcp__chrome-devtools__list_pages",
            description="list pages",
            parameters={"type": "object"},
            handler=lambda: "1: https://example.com [selected]",
        )
    )
    controller = BrowserController(
        BrowserSession(),
        manager,  # type: ignore[arg-type]
        registry,
        FakeConnectivity(BrowserProbe(False, error="offline")),
    )

    assert "isolated" in controller.tabs()
    controller.session.switch_to_shared("autoConnect")
    assert "https://example.com" in controller.tabs()


def test_browser_slash_command_routes_subcommands():
    manager = FakeManager()
    controller = _controller(manager)

    assert "Browser session" in handle_browser_command("/browser", controller)
    assert "integer" in handle_browser_command(
        "/browser connect nope",
        controller,
    )
    assert "usage:" in handle_browser_command("/browser unknown", controller)
