from __future__ import annotations

import json
from typing import Any

from stellarcode.command_policy import is_safe_read_only_command


class ApprovalPolicy:
    """Static, deterministic risk policy for tool execution."""

    SAFE_MCP_PREFIXES = ("mcp__chrome-devtools__",)

    DANGEROUS_TOOLS = frozenset(
        {
            "write_file",
            "delete_file",
            "execute_command",
            "create_project",
        }
    )

    _DANGER_LEVELS = {
        "execute_command": "high",
        "delete_file": "high",
        "write_file": "medium",
        "create_project": "medium",
    }

    _RISK_DESCRIPTIONS = {
        "execute_command": (
            "将执行系统命令，可能修改文件、安装软件或改变系统状态。"
        ),
        "delete_file": "将永久删除磁盘上的文件。",
        "write_file": "将写入或覆盖文件内容，原有内容可能丢失。",
        "create_project": "将在磁盘上创建文件和目录。",
    }

    @classmethod
    def requires_approval(
        cls,
        tool_name: str,
        arguments: str | dict[str, Any] | None = None,
    ) -> bool:
        if tool_name.startswith(cls.SAFE_MCP_PREFIXES):
            return False
        if tool_name == "execute_command" and _is_safe_command_arguments(arguments):
            return False
        return tool_name in cls.DANGEROUS_TOOLS or tool_name.startswith("mcp__")

    @classmethod
    def danger_level(
        cls,
        tool_name: str,
        arguments: str | dict[str, Any] | None = None,
    ) -> str:
        if tool_name.startswith(cls.SAFE_MCP_PREFIXES):
            return "safe"
        if tool_name == "execute_command" and _is_safe_command_arguments(arguments):
            return "safe"
        if tool_name.startswith("mcp__"):
            return "medium"
        return cls._DANGER_LEVELS.get(tool_name, "safe")

    @classmethod
    def risk_description(
        cls,
        tool_name: str,
        arguments: str | dict[str, Any] | None = None,
    ) -> str:
        if tool_name.startswith(cls.SAFE_MCP_PREFIXES):
            return "Chrome DevTools browser tool configured to run without approval."
        if tool_name == "execute_command" and _is_safe_command_arguments(arguments):
            return "Read-only environment or executable inspection command."
        if tool_name.startswith("mcp__"):
            return "第三方 MCP 工具可能访问外部系统或敏感数据。"
        return cls._RISK_DESCRIPTIONS.get(tool_name, "只读操作。")


def _is_safe_command_arguments(
    arguments: str | dict[str, Any] | None,
) -> bool:
    try:
        parsed = json.loads(arguments) if isinstance(arguments, str) else arguments
    except json.JSONDecodeError:
        return False
    if not isinstance(parsed, dict):
        return False
    command = parsed.get("command")
    if not isinstance(command, (str, list)):
        return False
    return is_safe_read_only_command(command)
