from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from stellarcode import __version__
from stellarcode.image import ImageProcessor
from stellarcode.mcp.jsonrpc import JsonRpcClient
from stellarcode.mcp.schema import sanitize_input_schema
from stellarcode.mcp.transport import MCP_PROTOCOL_VERSION, McpTransport
from stellarcode.tools import ToolOutput


@dataclass(frozen=True)
class McpToolDescriptor:
    server_name: str
    name: str
    namespaced_name: str
    description: str
    input_schema: dict[str, Any]


class McpClient:
    def __init__(self, server_name: str, transport: McpTransport) -> None:
        self.server_name = server_name
        self.transport = transport
        self.rpc = JsonRpcClient(transport)
        self.server_capabilities: dict[str, Any] = {}

    def initialize(self, timeout_seconds: float = 60) -> None:
        result = self.rpc.request(
            "initialize",
            {
                "protocolVersion": MCP_PROTOCOL_VERSION,
                "capabilities": {"tools": {}},
                "clientInfo": {"name": "stellarcode", "version": __version__},
            },
            timeout_seconds=timeout_seconds,
        )
        if not isinstance(result, dict):
            raise RuntimeError("MCP initialize returned a non-object result.")
        capabilities = result.get("capabilities")
        self.server_capabilities = capabilities if isinstance(capabilities, dict) else {}
        self.rpc.notify("notifications/initialized", {})

    def list_tools(self) -> list[McpToolDescriptor]:
        result = self.rpc.request("tools/list", {}, timeout_seconds=30)
        raw_tools = result.get("tools") if isinstance(result, dict) else None
        if not isinstance(raw_tools, list):
            return []
        descriptors: list[McpToolDescriptor] = []
        for raw in raw_tools:
            if not isinstance(raw, dict):
                continue
            name = str(raw.get("name") or "").strip()
            if not name:
                continue
            descriptors.append(
                McpToolDescriptor(
                    server_name=self.server_name,
                    name=name,
                    namespaced_name=namespaced_tool_name(self.server_name, name),
                    description=str(raw.get("description") or "").strip()[:1000],
                    input_schema=sanitize_input_schema(raw.get("inputSchema")),
                )
            )
        return descriptors

    def call_tool(self, tool_name: str, arguments: dict[str, Any]) -> str:
        return self.call_tool_output(tool_name, arguments).text

    def call_tool_output(
        self,
        tool_name: str,
        arguments: dict[str, Any],
    ) -> ToolOutput:
        result = self.rpc.request(
            "tools/call",
            {"name": tool_name, "arguments": arguments},
            timeout_seconds=60,
        )
        if not isinstance(result, dict):
            raise RuntimeError("MCP tools/call returned a non-object result.")
        content = result.get("content")
        chunks: list[str] = []
        image_parts: list[dict[str, Any]] = []
        if isinstance(content, list):
            for item in content:
                if not isinstance(item, dict):
                    continue
                if item.get("type") == "text":
                    chunks.append(str(item.get("text") or ""))
                elif item.get("type") == "image":
                    mime_type = str(item.get("mimeType") or "image/png")
                    data = str(item.get("data") or "")
                    try:
                        processed = ImageProcessor.from_base64(data, mime_type)
                        image_parts.append(processed.content_part())
                        chunks.append(
                            "[MCP returned image content. StellarCode attached it to the "
                            "next model turn; prefer take_snapshot when DOM text is needed.]"
                        )
                    except Exception as exc:
                        chunks.append(
                            "[MCP returned image content, but it could not be attached: "
                            f"{type(exc).__name__}: {exc}. Use take_snapshot for DOM text.]"
                        )
                else:
                    kind = str(item.get("type") or "non-text")
                    chunks.append(f"[MCP returned {kind} content; binary data omitted]")
        text = "\n".join(chunk for chunk in chunks if chunk).strip()
        structured = result.get("structuredContent")
        if not text and isinstance(structured, (dict, list)):
            text = json.dumps(structured, ensure_ascii=False, indent=2)
        if not text:
            text = "(MCP tool returned no text content)"
        if bool(result.get("isError")):
            text = f"MCP tool returned error: {text}"
        return ToolOutput(text, tuple(image_parts))

    def close(self) -> None:
        self.rpc.close()


def namespaced_tool_name(server_name: str, tool_name: str) -> str:
    server = _safe_name(server_name)
    tool = _safe_name(tool_name)
    return f"mcp__{server}__{tool}"


def _safe_name(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_-]+", "_", value.strip())
    return cleaned.strip("_") or "unnamed"
