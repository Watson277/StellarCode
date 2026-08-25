"""High-level browser commands layered over Chrome DevTools MCP and session policy."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from stellarcode.browser.connectivity import BrowserConnectivityCheck, BrowserProbe
from stellarcode.browser.session import BrowserMode, BrowserSession
from stellarcode.mcp.manager import McpServer, McpServerManager, McpServerStatus
from stellarcode.tools.registry import ToolDefinition, ToolExecutionError, ToolRegistry


CHROME_SERVER = "chrome-devtools"
CHROME_PACKAGE_ARGS = ["-y", "chrome-devtools-mcp@latest"]


class ConnectivityProbe(Protocol):
    def probe(self, port: int) -> BrowserProbe:
        ...


@dataclass
class BrowserController:
    session: BrowserSession
    manager: McpServerManager
    registry: ToolRegistry
    connectivity: ConnectivityProbe

    @classmethod
    def create(
        cls,
        session: BrowserSession,
        manager: McpServerManager,
        registry: ToolRegistry,
    ) -> BrowserController:
        controller = cls(session, manager, registry, BrowserConnectivityCheck())
        controller.sync_from_server()
        return controller

    def sync_from_server(self) -> None:
        server = self.manager.server(CHROME_SERVER)
        if server is None or server.status != McpServerStatus.READY:
            self.session.switch_to_isolated()
            return
        target = _shared_target(server)
        if target:
            self.session.switch_to_shared(target)
        else:
            self.session.switch_to_isolated()

    def status(self) -> str:
        server = self.manager.server(CHROME_SERVER)
        server_status = _server_status(server)
        if self.session.mode == BrowserMode.SHARED:
            mode = f"shared ({self.session.browser_url})"
        else:
            mode = "isolated (temporary profile, no existing login state)"
        probe = self.connectivity.probe(9222)
        legacy = probe.browser_url if probe.connected else f"unavailable: {probe.error}"
        return "\n".join(
            [
                "Browser session",
                f"- mode: {mode}",
                f"- chrome-devtools: {server_status}",
                f"- legacy CDP 9222: {legacy}",
                "- autoConnect: enable remote debugging at "
                "chrome://inspect/#remote-debugging, then run /browser connect",
            ]
        )

    def connect(self, port: int | None = None) -> str:
        server = self.manager.server(CHROME_SERVER)
        if server is None:
            return "chrome-devtools MCP server is not configured"
        if port is None:
            return self._switch_shared(
                CHROME_PACKAGE_ARGS + ["--autoConnect"],
                "autoConnect",
                "Enable remote debugging in chrome://inspect/#remote-debugging "
                "and approve Chrome's connection dialog.",
            )
        probe = self.connectivity.probe(port)
        if not probe.connected:
            return _legacy_connection_help(port, probe.error)
        return self._switch_shared(
            CHROME_PACKAGE_ARGS + [f"--browser-url={probe.browser_url}"],
            probe.browser_url,
            _legacy_connection_help(port, ""),
        )

    def disconnect(self) -> str:
        server = self.manager.server(CHROME_SERVER)
        if server is None:
            self.session.switch_to_isolated()
            return "chrome-devtools MCP server is not configured; local state reset"
        result = self.manager.restart_with_args(
            CHROME_SERVER,
            CHROME_PACKAGE_ARGS + ["--isolated=true"],
        )
        restarted = self.manager.server(CHROME_SERVER)
        if restarted is not None and restarted.status == McpServerStatus.READY:
            self.session.switch_to_isolated()
            return f"Switched browser to isolated mode.\n{result}"
        return f"Could not switch browser to isolated mode.\n{result}"

    def tabs(self) -> str:
        if self.session.mode != BrowserMode.SHARED:
            return (
                "The browser is isolated; existing Chrome tabs are unavailable. "
                "Run /browser connect first."
            )
        try:
            return self.registry.execute("mcp__chrome-devtools__list_pages", {})
        except ToolExecutionError as exc:
            return f"Could not list Chrome tabs: {exc}"

    def _switch_shared(
        self,
        args: list[str],
        target: str,
        failure_help: str,
    ) -> str:
        server = self.manager.server(CHROME_SERVER)
        if server is None:
            return "chrome-devtools MCP server is not configured"
        old_args = list(server.config.args)
        result = self.manager.restart_with_args(CHROME_SERVER, args)
        restarted = self.manager.server(CHROME_SERVER)
        if restarted is not None and restarted.status == McpServerStatus.READY:
            self.session.switch_to_shared(target)
            return f"Connected to shared Chrome using {target}.\n{result}"
        failure = restarted.error_message if restarted is not None else result
        rollback = self.manager.restart_with_args(CHROME_SERVER, old_args)
        self.sync_from_server()
        return (
            "Shared Chrome connection failed; restored the previous MCP arguments.\n"
            f"Reason: {failure}\n{failure_help}\nRollback: {rollback}"
        )


def register_browser_tools(registry: ToolRegistry, controller: BrowserController) -> None:
    registry.register(
        ToolDefinition(
            name="browser_status",
            description=(
                "Inspect whether Chrome DevTools uses an isolated browser or the user's "
                "shared Chrome session."
            ),
            parameters={"type": "object", "additionalProperties": False},
            handler=controller.status,
        )
    )
    registry.register(
        ToolDefinition(
            name="browser_connect",
            description=(
                "Switch chrome-devtools to the user's shared Chrome login session. "
                "Omit port for Chrome autoConnect; pass a port only for legacy CDP."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "port": {
                        "type": "integer",
                        "minimum": 1024,
                        "maximum": 65535,
                    }
                },
                "additionalProperties": False,
            },
            handler=lambda port=None: _agent_connect(controller, port),
        )
    )
    registry.register(
        ToolDefinition(
            name="browser_disconnect",
            description="Leave shared Chrome and restart chrome-devtools in isolated mode.",
            parameters={"type": "object", "additionalProperties": False},
            handler=lambda: _agent_disconnect(controller),
        )
    )
    registry.register(
        ToolDefinition(
            name="browser_tabs",
            description="List tabs from the current shared Chrome session.",
            parameters={"type": "object", "additionalProperties": False},
            handler=controller.tabs,
        )
    )


def _shared_target(server: McpServer) -> str:
    for argument in server.config.args:
        if argument in {"--autoConnect", "--auto-connect"}:
            return "autoConnect"
        if argument.startswith(("--browser-url=", "--browserUrl=")):
            return argument.split("=", 1)[1]
    return ""


def _agent_connect(controller: BrowserController, port: int | None = None) -> str:
    result = controller.connect(port)
    if controller.session.mode != BrowserMode.SHARED:
        raise ToolExecutionError(result)
    return result


def _agent_disconnect(controller: BrowserController) -> str:
    result = controller.disconnect()
    if controller.session.mode != BrowserMode.ISOLATED:
        raise ToolExecutionError(result)
    return result


def _server_status(server: McpServer | None) -> str:
    if server is None:
        return "not configured"
    status = f"{server.status.value} ({len(server.tools)} tools)"
    return f"{status}: {server.error_message}" if server.error_message else status


def _legacy_connection_help(port: int, error: str) -> str:
    prefix = (
        f"No Chrome CDP endpoint detected at 127.0.0.1:{port}: {error}\n"
        if error
        else "Legacy CDP startup reference:\n"
    )
    return prefix + "\n".join(
        [
            "Start Chrome with a separate debugging profile:",
            f'Windows: start chrome.exe --remote-debugging-port={port} '
            r'--user-data-dir=%TEMP%\stellarcode-chrome-profile',
            f'macOS: open -na "Google Chrome" --args --remote-debugging-port={port} '
            "--user-data-dir=/tmp/stellarcode-chrome-profile",
            f"Linux: google-chrome --remote-debugging-port={port} "
            "--user-data-dir=/tmp/stellarcode-chrome-profile",
            f"Then run /browser connect {port} again.",
        ]
    )
