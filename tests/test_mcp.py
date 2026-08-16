from __future__ import annotations

import json
import sys
import time
import base64
from io import BytesIO
from typing import Any

import httpx
import pytest
from PIL import Image

from stellarcode.cli import handle_mcp_command
from stellarcode.hitl import ApprovalPolicy
from stellarcode.mcp import (
    JsonRpcClient,
    JsonRpcError,
    McpAuditLog,
    McpClient,
    McpConfigError,
    McpConfigLoader,
    McpServerConfig,
    McpServerManager,
    McpServerStatus,
    McpTransport,
    StdioTransport,
    StreamableHttpTransport,
    sanitize_input_schema,
)
from stellarcode.tools import ToolDefinition, ToolExecutionError, ToolRegistry


class LoopbackTransport(McpTransport):
    def __init__(self, handlers: dict[str, Any]) -> None:
        self.handlers = handlers
        self.receiver = lambda _message: None
        self.sent: list[dict[str, Any]] = []
        self.closed = False

    @property
    def name(self) -> str:
        return "memory"

    def on_receive(self, receiver):
        self.receiver = receiver

    def send(self, message: dict[str, Any]) -> None:
        self.sent.append(message)
        if "id" not in message:
            return
        handler = self.handlers.get(message["method"])
        if isinstance(handler, BaseException):
            self.receiver(
                {
                    "jsonrpc": "2.0",
                    "id": message["id"],
                    "error": {"code": -32601, "message": str(handler)},
                }
            )
            return
        result = handler(message.get("params", {})) if callable(handler) else handler
        self.receiver({"jsonrpc": "2.0", "id": message["id"], "result": result})

    def close(self) -> None:
        self.closed = True


class SpyBrowserGuard:
    def __init__(self) -> None:
        self.before: list[tuple[str, dict[str, object]]] = []
        self.after: list[tuple[str, dict[str, object], str]] = []

    def before_call(self, name: str, arguments: dict[str, object]) -> None:
        self.before.append((name, arguments))

    def after_call(
        self,
        name: str,
        arguments: dict[str, object],
        result: str,
    ) -> None:
        self.after.append((name, arguments, result))


def test_config_loader_merges_project_over_user_and_expands_variables(
    tmp_path,
    monkeypatch,
):
    user = tmp_path / "user.json"
    project = tmp_path / "project.json"
    user.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "fs": {"command": "old", "args": []},
                    "remote": {"url": "https://example.test/mcp"},
                }
            }
        ),
        encoding="utf-8",
    )
    project.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "fs": {
                        "command": "runner",
                        "args": ["${PROJECT_DIR}"],
                        "env": {"TOKEN": "${MCP_TEST_TOKEN}"},
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("MCP_TEST_TOKEN", "secret")
    loader = McpConfigLoader(tmp_path, user, project)

    configs = loader.load()
    prepared = loader.prepare(configs["fs"])

    assert set(configs) == {"fs", "remote"}
    assert prepared.command == "runner"
    assert prepared.args == [str(tmp_path.resolve())]
    assert prepared.env == {"TOKEN": "secret"}


def test_config_bootstrap_creates_default_isolated_chrome_without_overwriting(tmp_path):
    user = tmp_path / "home" / ".stellarcode" / "mcp.json"
    loader = McpConfigLoader(tmp_path, user, tmp_path / "absent.json")

    message = loader.bootstrap_chrome_devtools()
    created = json.loads(user.read_text(encoding="utf-8"))

    assert "Created default MCP config" in message
    assert created["mcpServers"]["chrome-devtools"]["command"] == "npx"
    assert "--isolated=true" in created["mcpServers"]["chrome-devtools"]["args"]
    assert loader.bootstrap_chrome_devtools() == ""

    custom = {"mcpServers": {"weather": {"command": "python"}}}
    user.write_text(json.dumps(custom), encoding="utf-8")
    hint = loader.bootstrap_chrome_devtools()

    assert "does not configure chrome-devtools" in hint
    assert json.loads(user.read_text(encoding="utf-8")) == custom


def test_config_prepare_isolates_missing_variable_and_invalid_transport(tmp_path):
    config = tmp_path / "mcp.json"
    config.write_text(
        '{"mcpServers":{"missing":{"command":"${NO_MCP_TEST_VAR}"},"both":'
        '{"command":"x","url":"https://example.test"}}}',
        encoding="utf-8",
    )
    loader = McpConfigLoader(tmp_path, config, tmp_path / "absent.json")
    servers = loader.load()

    with pytest.raises(McpConfigError, match="NO_MCP_TEST_VAR"):
        loader.prepare(servers["missing"])
    with pytest.raises(McpConfigError, match="exactly one"):
        loader.prepare(servers["both"])


def test_project_config_crud_preserves_document_and_secret_placeholders(
    tmp_path,
    monkeypatch,
):
    project_config = tmp_path / ".stellarcode" / "mcp.json"
    project_config.parent.mkdir()
    project_config.write_text(
        json.dumps({"formatVersion": 1, "mcpServers": {"existing": {"command": "old"}}}),
        encoding="utf-8",
    )
    monkeypatch.setenv("MCP_DESKTOP_TOKEN", "must-not-be-written")
    loader = McpConfigLoader(
        tmp_path,
        tmp_path / "user.json",
        project_config,
    )

    installed = loader.install_project_server(
        "custom-server",
        McpServerConfig(
            command="runner",
            args=["${PROJECT_DIR}"],
            env={"TOKEN": "${MCP_DESKTOP_TOKEN}"},
        ),
    )
    stored = json.loads(project_config.read_text(encoding="utf-8"))

    assert installed.source == "project"
    assert stored["formatVersion"] == 1
    assert stored["mcpServers"]["existing"]["command"] == "old"
    assert stored["mcpServers"]["custom-server"]["env"]["TOKEN"] == "${MCP_DESKTOP_TOKEN}"
    assert "must-not-be-written" not in project_config.read_text(encoding="utf-8")
    assert loader.load()["custom-server"].source == "project"

    loader.remove_project_server("custom-server")
    remaining = json.loads(project_config.read_text(encoding="utf-8"))
    assert set(remaining["mcpServers"]) == {"existing"}


def test_project_config_rejects_invalid_server_names_and_http_urls(tmp_path):
    loader = McpConfigLoader(
        tmp_path,
        tmp_path / "user.json",
        tmp_path / ".stellarcode" / "mcp.json",
    )

    with pytest.raises(McpConfigError, match="letters"):
        loader.install_project_server("bad server", McpServerConfig(command="runner"))
    with pytest.raises(McpConfigError, match="http"):
        loader.install_project_server("remote", McpServerConfig(url="file:///tmp/mcp"))


def test_schema_sanitizer_removes_refs_and_flattens_alternatives():
    schema = sanitize_input_schema(
        {
            "$schema": "draft",
            "$ref": "#/$defs/value",
            "anyOf": [{"type": "string"}, {"type": "number"}],
        }
    )

    assert schema["type"] == "object"
    assert schema["properties"] == {}
    assert "$schema" not in schema
    assert "$ref" not in schema
    assert "anyOf" not in schema
    assert "anyOf options" in schema["description"]


def test_json_rpc_pairs_responses_and_maps_errors():
    transport = LoopbackTransport(
        {
            "ping": {"ok": True},
            "missing": RuntimeError("not found"),
        }
    )
    client = JsonRpcClient(transport)

    assert client.request("ping", {}, timeout_seconds=1) == {"ok": True}
    with pytest.raises(JsonRpcError) as error:
        client.request("missing", {}, timeout_seconds=1)

    assert error.value.code == -32601
    assert transport.sent[0]["id"] == 1
    assert transport.sent[1]["id"] == 2


def test_mcp_client_handshake_lists_tools_and_calls_tool():
    transport = LoopbackTransport(
        {
            "initialize": {
                "protocolVersion": "2025-03-26",
                "capabilities": {"tools": {"listChanged": True}},
            },
            "tools/list": {
                "tools": [
                    {
                        "name": "echo",
                        "description": "Echo text",
                        "inputSchema": {
                            "type": "object",
                            "properties": {"text": {"type": "string"}},
                            "required": ["text"],
                        },
                    }
                ]
            },
            "tools/call": lambda params: {
                "content": [{"type": "text", "text": f"echo:{params['arguments']['text']}"}],
                "isError": False,
            },
        }
    )
    client = McpClient("demo", transport)

    client.initialize()
    tools = client.list_tools()
    result = client.call_tool("echo", {"text": "hello"})

    assert transport.sent[0]["method"] == "initialize"
    assert transport.sent[1]["method"] == "notifications/initialized"
    assert "id" not in transport.sent[1]
    assert tools[0].namespaced_name == "mcp__demo__echo"
    assert result == "echo:hello"


def test_mcp_client_guides_image_results_to_snapshot_and_uses_structured_fallback():
    image_bytes = BytesIO()
    Image.new("RGB", (2, 2), "blue").save(image_bytes, format="PNG")
    encoded_image = base64.b64encode(image_bytes.getvalue()).decode("ascii")
    image_transport = LoopbackTransport(
        {
            "tools/call": {
                "content": [
                    {
                        "type": "image",
                        "data": encoded_image,
                        "mimeType": "image/png",
                    }
                ],
                "isError": False,
            }
        }
    )
    structured_transport = LoopbackTransport(
        {
            "tools/call": {
                "content": [],
                "structuredContent": {"ok": True},
                "isError": False,
            }
        }
    )

    image_result = McpClient("chrome-devtools", image_transport).call_tool_output(
        "shot", {}
    )
    structured_result = McpClient("demo", structured_transport).call_tool("data", {})

    assert "take_snapshot" in image_result.text
    assert len(image_result.image_parts) == 1
    assert image_result.image_parts[0]["type"] == "image_url"
    assert '"ok": true' in structured_result


def test_startup_reports_slow_server_progress(tmp_path):
    config = tmp_path / "mcp.json"
    config.write_text(
        '{"mcpServers":{"slow":{"command":"memory"}}}',
        encoding="utf-8",
    )

    def slow_initialize(_params):
        time.sleep(0.04)
        return {"capabilities": {"tools": {}}}

    transport = LoopbackTransport(
        {
            "initialize": slow_initialize,
            "tools/list": {"tools": []},
        }
    )
    manager = McpServerManager(
        ToolRegistry(),
        tmp_path,
        config_loader=McpConfigLoader(tmp_path, config, tmp_path / "absent.json"),
        transport_factory=lambda _config, _project: transport,
    )
    messages: list[str] = []

    manager.load_configured_servers()
    manager.start_all(progress=messages.append, progress_interval_seconds=0.01)

    assert any("starting" in message for message in messages)
    assert any("still starting" in message for message in messages)
    assert any("ready" in message for message in messages)


def test_streamable_http_parses_sse_reuses_session_and_deletes_on_close():
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.method == "DELETE":
            return httpx.Response(200, request=request)
        payload = json.loads(request.content)
        return httpx.Response(
            200,
                text=(
                    f'data: {{"jsonrpc":"2.0","id":{payload["id"]},'
                    '"result":{"ok":true}}\n\n'
                ),
            headers={
                "content-type": "text/event-stream",
                "Mcp-Session-Id": "session-123",
            },
            request=request,
        )

    transport = StreamableHttpTransport(
        "https://example.test/mcp",
        {"Authorization": "Bearer test"},
        transport=httpx.MockTransport(handler),
    )
    received: list[dict[str, Any]] = []
    transport.on_receive(received.append)

    transport.send({"jsonrpc": "2.0", "id": 1, "method": "ping"})
    transport.send({"jsonrpc": "2.0", "id": 2, "method": "ping"})
    transport.close()

    assert received[0]["result"] == {"ok": True}
    assert requests[0].headers["MCP-Protocol-Version"] == "2025-03-26"
    assert "Mcp-Session-Id" not in requests[0].headers
    assert requests[1].headers["Mcp-Session-Id"] == "session-123"
    assert requests[2].method == "DELETE"


def test_real_stdio_server_end_to_end(tmp_path):
    server_code = r"""
import json, sys
for line in sys.stdin:
    message = json.loads(line)
    if "id" not in message:
        continue
    method = message["method"]
    if method == "initialize":
        result = {"protocolVersion":"2025-03-26","capabilities":{"tools":{}}}
    elif method == "tools/list":
        result = {"tools":[{"name":"echo","description":"Echo","inputSchema":{"type":"object","properties":{"text":{"type":"string"}}}}]}
    elif method == "tools/call":
        result = {"content":[{"type":"text","text":"stdio:" + message["params"]["arguments"]["text"]}],"isError":False}
    else:
        result = {}
    print(json.dumps({"jsonrpc":"2.0","id":message["id"],"result":result}), flush=True)
"""
    transport = StdioTransport(
        sys.executable,
        ["-u", "-c", server_code],
        working_dir=tmp_path,
    )
    client = McpClient("stdio_test", transport)

    client.initialize()
    tools = client.list_tools()
    result = client.call_tool("echo", {"text": "works"})
    client.close()

    assert tools[0].namespaced_name == "mcp__stdio_test__echo"
    assert result == "stdio:works"


def test_server_manager_registers_invokes_and_unloads_namespaced_tools(tmp_path):
    config = tmp_path / "mcp.json"
    config.write_text(
        '{"mcpServers":{"demo":{"command":"memory"}}}',
        encoding="utf-8",
    )
    transport = LoopbackTransport(
        {
            "initialize": {"capabilities": {"tools": {}}},
            "tools/list": {
                "tools": [
                    {
                        "name": "echo",
                        "description": "Echo",
                        "inputSchema": {
                            "type": "object",
                            "properties": {"text": {"type": "string"}},
                        },
                    }
                ]
            },
            "tools/call": lambda params: {
                "content": [{"type": "text", "text": params["arguments"]["text"]}],
                "isError": False,
            },
        }
    )
    registry = ToolRegistry()
    browser_guard = SpyBrowserGuard()
    manager = McpServerManager(
        registry,
        tmp_path,
        config_loader=McpConfigLoader(tmp_path, config, tmp_path / "absent.json"),
        transport_factory=lambda _config, _project: transport,
        audit_log=McpAuditLog(tmp_path / "audit.jsonl"),
        browser_guard=browser_guard,  # type: ignore[arg-type]
    )

    manager.load_configured_servers()
    manager.start_all()

    assert manager.server("demo").status == McpServerStatus.READY
    assert registry.execute(
        "mcp__demo__echo",
        {"text": "hello", "api_token": "do-not-log"},
    ) == "hello"
    assert "1 tools" in manager.format_status()
    audit = json.loads((tmp_path / "audit.jsonl").read_text(encoding="utf-8"))
    assert audit["namespaced_tool"] == "mcp__demo__echo"
    assert audit["status"] == "ok"
    assert audit["arguments"]["api_token"] == "[REDACTED]"
    assert browser_guard.before == [
        ("mcp__demo__echo", {"text": "hello", "api_token": "do-not-log"})
    ]
    assert browser_guard.after == [
        (
            "mcp__demo__echo",
            {"text": "hello", "api_token": "do-not-log"},
            "hello",
        )
    ]

    manager.disable("demo")
    with pytest.raises(ToolExecutionError, match="Unknown tool"):
        registry.execute("mcp__demo__echo", {"text": "hello"})


def test_server_manager_project_management_updates_snapshot_and_registry(
    tmp_path,
    monkeypatch,
):
    transport = LoopbackTransport(
        {
            "initialize": {"capabilities": {"tools": {}}},
            "tools/list": {
                "tools": [
                    {
                        "name": "echo",
                        "description": "Echo text",
                        "inputSchema": {"type": "object", "properties": {}},
                    }
                ]
            },
        }
    )
    registry = ToolRegistry()
    statuses: list[str] = []
    loader = McpConfigLoader(
        tmp_path,
        tmp_path / "user.json",
        tmp_path / ".stellarcode" / "mcp.json",
    )
    manager = McpServerManager(
        registry,
        tmp_path,
        config_loader=loader,
        transport_factory=lambda _config, _project: transport,
        status_callback=lambda server: statuses.append(server.status.value),
    )
    manager.load_configured_servers()
    monkeypatch.setenv("MCP_MANAGER_TOKEN", "runtime-secret")

    manager.install(
        "demo",
        McpServerConfig(
            command="memory",
            env={"CUSTOM": "${MCP_MANAGER_TOKEN}"},
        ),
    )
    ready = manager.snapshot()

    assert ready["ready_servers"] == 1
    assert ready["total_tools"] == 1
    assert ready["servers"][0]["tools"][0]["namespaced_name"] == "mcp__demo__echo"
    assert ready["servers"][0]["env_keys"] == ["CUSTOM"]
    assert "runtime-secret" not in json.dumps(ready)
    assert any(tool.name == "mcp__demo__echo" for tool in registry.list_tools())

    manager.set_enabled("demo", False)
    assert manager.snapshot()["servers"][0]["status"] == "disabled"
    assert all(tool.name != "mcp__demo__echo" for tool in registry.list_tools())
    stored = json.loads(loader.project_config.read_text(encoding="utf-8"))
    assert stored["mcpServers"]["demo"]["disabled"] is True
    assert stored["mcpServers"]["demo"]["env"]["CUSTOM"] == "${MCP_MANAGER_TOKEN}"

    manager.set_enabled("demo", True)
    assert manager.snapshot()["servers"][0]["status"] == "ready"
    assert any(tool.name == "mcp__demo__echo" for tool in registry.list_tools())

    manager.remove_project_server("demo")
    assert manager.snapshot()["servers"] == []
    assert all(tool.name != "mcp__demo__echo" for tool in registry.list_tools())
    assert "starting" in statuses
    assert "ready" in statuses
    assert "disabled" in statuses


def test_one_bad_server_does_not_block_a_good_server(tmp_path):
    config = tmp_path / "mcp.json"
    config.write_text(
        '{"mcpServers":{"good":{"command":"memory"},'
        '"bad":{"command":"${MISSING_MCP_SERVER_VAR}"}}}',
        encoding="utf-8",
    )
    good_transport = LoopbackTransport(
        {
            "initialize": {"capabilities": {"tools": {}}},
            "tools/list": {"tools": []},
        }
    )
    manager = McpServerManager(
        ToolRegistry(),
        tmp_path,
        config_loader=McpConfigLoader(tmp_path, config, tmp_path / "absent.json"),
        transport_factory=lambda _config, _project: good_transport,
    )

    manager.load_configured_servers()
    manager.start_all()

    assert manager.server("good").status == McpServerStatus.READY
    assert manager.server("bad").status == McpServerStatus.ERROR
    assert "MISSING_MCP_SERVER_VAR" in manager.server("bad").error_message


def test_partial_tool_registration_is_rolled_back(tmp_path):
    config = tmp_path / "mcp.json"
    config.write_text(
        '{"mcpServers":{"demo":{"command":"memory"}}}',
        encoding="utf-8",
    )
    transport = LoopbackTransport(
        {
            "initialize": {"capabilities": {"tools": {}}},
            "tools/list": {
                "tools": [
                    {"name": "first", "inputSchema": {"type": "object"}},
                    {"name": "collision", "inputSchema": {"type": "object"}},
                ]
            },
        }
    )
    registry = ToolRegistry()
    registry.register(
        ToolDefinition(
            name="mcp__demo__collision",
            description="existing",
            parameters={"type": "object"},
            handler=lambda: "existing",
        )
    )
    manager = McpServerManager(
        registry,
        tmp_path,
        config_loader=McpConfigLoader(tmp_path, config, tmp_path / "absent.json"),
        transport_factory=lambda _config, _project: transport,
    )

    manager.load_configured_servers()
    manager.start_all()

    assert manager.server("demo").status == McpServerStatus.ERROR
    with pytest.raises(ToolExecutionError, match="Unknown tool"):
        registry.execute("mcp__demo__first", {})
    assert registry.execute("mcp__demo__collision", {}) == "existing"


def test_mcp_tools_require_approval_and_cli_commands_route_to_manager():
    assert ApprovalPolicy.requires_approval("mcp__demo__echo")
    assert ApprovalPolicy.danger_level("mcp__demo__echo") == "medium"

    class FakeManager:
        def format_status(self):
            return "status"

        def restart(self, name):
            return f"restart:{name}"

        def logs(self, name):
            return f"logs:{name}"

        def disable(self, name):
            return f"disable:{name}"

        def enable(self, name):
            return f"enable:{name}"

    manager = FakeManager()
    assert handle_mcp_command("/mcp", manager) == "status"
    assert handle_mcp_command("/mcp restart demo", manager) == "restart:demo"
    assert handle_mcp_command("/mcp logs demo", manager) == "logs:demo"
    assert handle_mcp_command("/mcp disable demo", manager) == "disable:demo"
    assert handle_mcp_command("/mcp enable demo", manager) == "enable:demo"
