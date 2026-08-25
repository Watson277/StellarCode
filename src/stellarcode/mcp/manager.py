"""MCP server lifecycle and dynamic ToolRegistry integration.

An MCP server's discovered tools become normal namespaced tool definitions. The Agent
therefore sees MCP capabilities through the same Function Calling schema and safety
pipeline as built-in tools.
"""

from __future__ import annotations

import threading
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

from stellarcode.mcp.audit import McpAuditLog
from stellarcode.mcp.client import McpClient, McpToolDescriptor
from stellarcode.mcp.config import McpConfigLoader, McpServerConfig
from stellarcode.mcp.transport import (
    McpTransport,
    StdioTransport,
    StreamableHttpTransport,
)
from stellarcode.tools.registry import (
    ToolDefinition,
    ToolExecutionError,
    ToolOutput,
    ToolRegistry,
)

if TYPE_CHECKING:
    from stellarcode.browser.guard import BrowserGuard


class McpServerStatus(str, Enum):
    STARTING = "starting"
    READY = "ready"
    DISABLED = "disabled"
    ERROR = "error"


@dataclass
class McpServer:
    name: str
    config: McpServerConfig
    status: McpServerStatus = McpServerStatus.DISABLED
    client: McpClient | None = None
    tools: list[McpToolDescriptor] = field(default_factory=list)
    error_message: str = ""
    stderr_log: list[str] = field(default_factory=list)
    started_at: float = 0.0
    startup_started_at: float = 0.0
    lock: threading.RLock = field(default_factory=threading.RLock, repr=False)

    @property
    def uptime_seconds(self) -> int:
        if self.status != McpServerStatus.READY or not self.started_at:
            return 0
        return max(0, int(time.monotonic() - self.started_at))


TransportFactory = Callable[[McpServerConfig, Path], McpTransport]
StatusCallback = Callable[[McpServer], None]


class McpServerManager:
    def __init__(
        self,
        tool_registry: ToolRegistry,
        project_dir: str | Path,
        config_loader: McpConfigLoader | None = None,
        transport_factory: TransportFactory | None = None,
        audit_log: McpAuditLog | None = None,
        browser_guard: BrowserGuard | None = None,
        status_callback: StatusCallback | None = None,
    ) -> None:
        self.tool_registry = tool_registry
        self.project_dir = Path(project_dir).resolve()
        self.config_loader = config_loader or McpConfigLoader(self.project_dir)
        self.transport_factory = transport_factory or _create_transport
        self.audit_log = audit_log or McpAuditLog(
            self.project_dir / ".stellarcode" / "audit" / "mcp-tools.jsonl"
        )
        self.browser_guard = browser_guard
        self.status_callback = status_callback
        self._servers: dict[str, McpServer] = {}
        self._lock = threading.RLock()

    def load_configured_servers(self) -> None:
        configs = self.config_loader.load()
        with self._lock:
            self._servers = {
                name: McpServer(
                    name=name,
                    config=config,
                    status=(
                        McpServerStatus.DISABLED
                        if config.disabled
                        else McpServerStatus.STARTING
                    ),
                )
                for name, config in configs.items()
            }
            servers = list(self._servers.values())
        for server in servers:
            self._notify(server)

    def start_all(
        self,
        progress: Callable[[str], None] | None = None,
        progress_interval_seconds: float = 5.0,
    ) -> None:
        targets = [server for server in self.servers() if not server.config.disabled]
        if not targets:
            return
        if progress_interval_seconds <= 0:
            raise ValueError("progress_interval_seconds must be greater than zero")
        submitted_at = time.monotonic()
        for server in targets:
            server.startup_started_at = submitted_at
        if progress:
            for server in targets:
                note = (
                    "; first launch may download npm packages and start Chrome"
                    if server.name == "chrome-devtools"
                    else ""
                )
                progress(
                    f"MCP {server.name}: starting ({server.config.transport_name}{note})"
                )
        with ThreadPoolExecutor(
            max_workers=min(len(targets), 8),
            thread_name_prefix="stellarcode-mcp-startup",
        ) as executor:
            futures = {
                executor.submit(self._start, server): server for server in targets
            }
            pending = set(futures)
            next_progress = time.monotonic() + progress_interval_seconds
            while pending:
                timeout = max(0.0, next_progress - time.monotonic())
                done, pending = wait(
                    pending,
                    timeout=timeout,
                    return_when=FIRST_COMPLETED,
                )
                for future in done:
                    future.result()
                    if progress:
                        progress(self._startup_result(futures[future]))
                now = time.monotonic()
                if pending and progress and now >= next_progress:
                    for future in sorted(pending, key=lambda item: futures[item].name):
                        server = futures[future]
                        elapsed = int(now - server.startup_started_at)
                        progress(f"MCP {server.name}: still starting ({elapsed}s elapsed)")
                    next_progress = now + progress_interval_seconds

    def servers(self) -> list[McpServer]:
        with self._lock:
            return sorted(self._servers.values(), key=lambda server: server.name.lower())

    def server(self, name: str) -> McpServer | None:
        with self._lock:
            return self._servers.get(name)

    def restart(self, name: str) -> str:
        server = self.server(name)
        if server is None:
            return f"MCP server not found: {name}"
        server.config.disabled = False
        self._start(server)
        if server.status == McpServerStatus.READY:
            return f"MCP server restarted: {name}"
        return f"MCP server restart failed: {name} - {server.error_message}"

    def install(
        self,
        name: str,
        config: McpServerConfig,
        *,
        overwrite: bool = False,
    ) -> McpServer:
        self.config_loader.install_project_server(name, config, overwrite=overwrite)
        server = self.reload(name)
        if server is None:
            raise RuntimeError(f"MCP server disappeared after installation: {name}")
        return server

    def set_enabled(self, name: str, enabled: bool) -> McpServer:
        server = self.server(name)
        if server is None:
            raise ValueError(f"MCP server not found: {name}")
        config = McpServerConfig(
            command=server.config.command,
            args=list(server.config.args),
            env=dict(server.config.env),
            url=server.config.url,
            headers=dict(server.config.headers),
            disabled=not enabled,
            source="project",
        )
        self.config_loader.install_project_server(name, config, overwrite=True)
        reloaded = self.reload(name)
        if reloaded is None:
            raise RuntimeError(f"MCP server disappeared after update: {name}")
        return reloaded

    def remove_project_server(self, name: str) -> McpServer | None:
        server = self.server(name)
        if server is None:
            raise ValueError(f"MCP server not found: {name}")
        if server.config.source != "project":
            raise ValueError(
                f"MCP server {name} comes from the user config and cannot be removed "
                "from this project. Disable it to create a project override."
            )
        self.config_loader.remove_project_server(name)
        return self.reload(name)

    def reload(self, name: str) -> McpServer | None:
        configs = self.config_loader.load()
        config = configs.get(name)
        old = self.server(name)
        if old is not None:
            with old.lock:
                self._unregister_tools(old)
                self._close_client(old)
        with self._lock:
            if config is None:
                self._servers.pop(name, None)
                return None
            server = McpServer(
                name=name,
                config=config,
                status=(
                    McpServerStatus.DISABLED
                    if config.disabled
                    else McpServerStatus.STARTING
                ),
            )
            self._servers[name] = server
        self._notify(server)
        if not config.disabled:
            self._start(server)
        return server

    def snapshot(self) -> dict[str, Any]:
        servers = [self.server_snapshot(server) for server in self.servers()]
        return {
            "servers": servers,
            "ready_servers": sum(1 for server in servers if server["status"] == "ready"),
            "total_servers": len(servers),
            "total_tools": sum(int(server["tool_count"]) for server in servers),
            "project_config_path": str(self.config_loader.project_config),
            "user_config_path": str(self.config_loader.user_config),
        }

    @staticmethod
    def server_snapshot(server: McpServer) -> dict[str, Any]:
        with server.lock:
            process_id = (
                server.client.transport.process_id
                if server.client is not None
                else None
            )
            capabilities = (
                sorted(server.client.server_capabilities)
                if server.client is not None
                else []
            )
            return {
                "name": server.name,
                "status": server.status.value,
                "transport": server.config.transport_name,
                "source": server.config.source or "unknown",
                "disabled": server.config.disabled,
                "command": server.config.command,
                "args": list(server.config.args),
                "url": server.config.url,
                "env_keys": sorted(server.config.env),
                "header_keys": sorted(server.config.headers),
                "tool_count": len(server.tools),
                "tools": [
                    {
                        "name": tool.name,
                        "namespaced_name": tool.namespaced_name,
                        "description": tool.description,
                        "input_schema": tool.input_schema,
                    }
                    for tool in server.tools
                ],
                "error": server.error_message,
                "uptime_seconds": server.uptime_seconds,
                "process_id": process_id,
                "capabilities": capabilities,
            }

    def restart_with_args(self, name: str, args: list[str]) -> str:
        server = self.server(name)
        if server is None:
            return f"MCP server not found: {name}"
        server.config.args = list(args)
        return self.restart(name)

    def disable(self, name: str) -> str:
        server = self.server(name)
        if server is None:
            return f"MCP server not found: {name}"
        with server.lock:
            server.config.disabled = True
            self._unregister_tools(server)
            self._close_client(server)
            server.status = McpServerStatus.DISABLED
            server.error_message = ""
            self._notify(server)
        return f"MCP server disabled: {name}"

    def enable(self, name: str) -> str:
        server = self.server(name)
        if server is None:
            return f"MCP server not found: {name}"
        server.config.disabled = False
        self._start(server)
        if server.status == McpServerStatus.READY:
            return f"MCP server enabled: {name}"
        return f"MCP server enable failed: {name} - {server.error_message}"

    def logs(self, name: str) -> str:
        server = self.server(name)
        if server is None:
            return f"MCP server not found: {name}"
        if server.client is None:
            if server.stderr_log:
                return "\n".join(server.stderr_log)
            return server.error_message or f"No MCP stderr logs: {name}"
        lines = server.client.transport.stderr_lines()
        return "\n".join(lines) if lines else f"No MCP stderr logs: {name}"

    def format_status(self) -> str:
        servers = self.servers()
        if not servers:
            return (
                "MCP Servers\n"
                "  No servers configured. Use ~/.stellarcode/mcp.json or .stellarcode/mcp.json."
            )
        lines = ["MCP Servers"]
        for server in servers:
            details = [
                server.status.value,
                server.config.transport_name,
                f"{len(server.tools)} tools",
            ]
            if server.status == McpServerStatus.READY:
                details.append(f"uptime {_format_duration(server.uptime_seconds)}")
                if server.client and server.client.transport.process_id:
                    details.append(f"pid {server.client.transport.process_id}")
            if server.error_message:
                details.append(server.error_message)
            lines.append(f"- {server.name}: " + " | ".join(details))
        return "\n".join(lines)

    def close(self) -> None:
        for server in self.servers():
            with server.lock:
                self._unregister_tools(server)
                self._close_client(server)

    def _start(self, server: McpServer) -> None:
        with server.lock:
            self._unregister_tools(server)
            self._close_client(server)
            if server.config.disabled:
                server.status = McpServerStatus.DISABLED
                return
            server.status = McpServerStatus.STARTING
            server.error_message = ""
            server.stderr_log = []
            server.startup_started_at = time.monotonic()
            self._notify(server)
            client: McpClient | None = None
            registered_names: list[str] = []
            try:
                config = self.config_loader.prepare(server.config)
                transport = self.transport_factory(config, self.project_dir)
                client = McpClient(server.name, transport)
                client.initialize()
                tools = client.list_tools()
                self._validate_tool_names(server.name, tools)
                for descriptor in tools:
                    self.tool_registry.register(
                        ToolDefinition(
                            name=descriptor.namespaced_name,
                            description=(
                                f"MCP server '{server.name}' tool '{descriptor.name}'. "
                                f"{descriptor.description}"
                            ).strip(),
                            parameters=descriptor.input_schema,
                            handler=self._tool_handler(client, descriptor),
                        )
                    )
                    registered_names.append(descriptor.namespaced_name)
                server.client = client
                server.tools = tools
                server.started_at = time.monotonic()
                server.status = McpServerStatus.READY
            except Exception as exc:
                for name in registered_names:
                    self.tool_registry.unregister(name)
                if client is not None:
                    server.stderr_log = client.transport.stderr_lines()
                    client.close()
                server.client = None
                server.tools = []
                server.error_message = f"{type(exc).__name__}: {exc}"
                server.status = McpServerStatus.ERROR
            self._notify(server)

    def _notify(self, server: McpServer) -> None:
        if self.status_callback is None:
            return
        try:
            self.status_callback(server)
        except Exception:
            pass

    @staticmethod
    def _startup_result(server: McpServer) -> str:
        elapsed = max(0.0, time.monotonic() - server.startup_started_at)
        if server.status == McpServerStatus.READY:
            return (
                f"MCP {server.name}: ready ({server.config.transport_name}, "
                f"{len(server.tools)} tools, {elapsed:.1f}s)"
            )
        return f"MCP {server.name}: error ({server.error_message})"

    def _tool_handler(
        self,
        client: McpClient,
        descriptor: McpToolDescriptor,
    ) -> Callable[..., str | ToolOutput]:
        def invoke(**arguments: object) -> str | ToolOutput:
            started_at = time.monotonic()
            try:
                if self.browser_guard is not None:
                    self.browser_guard.before_call(
                        descriptor.namespaced_name,
                        arguments,
                    )
                output = client.call_tool_output(descriptor.name, dict(arguments))
                result = output.text
                if self.browser_guard is not None:
                    self.browser_guard.after_call(
                        descriptor.namespaced_name,
                        arguments,
                        result,
                    )
            except Exception as exc:
                self._record_audit(descriptor, arguments, "error", started_at, str(exc))
                raise ToolExecutionError(
                    f"MCP tool failed ({descriptor.server_name}/{descriptor.name}): {exc}"
                ) from exc
            if result.startswith("MCP tool returned error:"):
                self._record_audit(descriptor, arguments, "error", started_at, result)
                raise ToolExecutionError(result)
            self._record_audit(descriptor, arguments, "ok", started_at)
            return output

        return invoke

    def _record_audit(
        self,
        descriptor: McpToolDescriptor,
        arguments: dict[str, object],
        status: str,
        started_at: float,
        error: str = "",
    ) -> None:
        self.audit_log.record(
            server=descriptor.server_name,
            tool=descriptor.name,
            namespaced_tool=descriptor.namespaced_name,
            arguments=arguments,
            status=status,
            elapsed_ms=int((time.monotonic() - started_at) * 1000),
            error=error,
        )

    @staticmethod
    def _validate_tool_names(
        server_name: str,
        tools: list[McpToolDescriptor],
    ) -> None:
        seen: set[str] = set()
        duplicates: set[str] = set()
        for tool in tools:
            if tool.namespaced_name in seen:
                duplicates.add(tool.namespaced_name)
            seen.add(tool.namespaced_name)
        if duplicates:
            raise ValueError(
                f"MCP server {server_name} returned duplicate tool names: "
                + ", ".join(sorted(duplicates))
            )

    def _unregister_tools(self, server: McpServer) -> None:
        for descriptor in server.tools:
            self.tool_registry.unregister(descriptor.namespaced_name)
        server.tools = []

    @staticmethod
    def _close_client(server: McpServer) -> None:
        if server.client is not None:
            server.client.close()
            server.client = None


def _create_transport(config: McpServerConfig, project_dir: Path) -> McpTransport:
    if config.is_http:
        return StreamableHttpTransport(config.url, config.headers)
    return StdioTransport(config.command, config.args, config.env, project_dir)


def _format_duration(seconds: int) -> str:
    if seconds < 60:
        return f"{seconds}s"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes}m"
    return f"{minutes // 60}h"
