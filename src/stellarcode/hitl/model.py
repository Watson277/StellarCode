from __future__ import annotations

import json
from dataclasses import dataclass
from enum import Enum
from typing import Any

from stellarcode.hitl.policy import ApprovalPolicy


class Decision(str, Enum):
    APPROVED = "approved"
    APPROVED_ALL = "approved_all"
    REJECTED = "rejected"
    MODIFIED = "modified"
    SKIPPED = "skipped"


@dataclass(frozen=True)
class ApprovalRequest:
    tool_name: str
    arguments: str
    danger_level: str
    risk_description: str
    suggestion: str | None = None
    caller_context: str | None = None
    tool_call_id: str | None = None
    change_preview: dict[str, Any] | None = None
    display_arguments: dict[str, Any] | None = None

    @classmethod
    def create(
        cls,
        tool_name: str,
        arguments: str,
        suggestion: str | None = None,
        caller_context: str | None = None,
        tool_call_id: str | None = None,
        change_preview: dict[str, Any] | None = None,
        display_arguments: dict[str, Any] | None = None,
    ) -> "ApprovalRequest":
        return cls(
            tool_name=tool_name,
            arguments=arguments,
            danger_level=ApprovalPolicy.danger_level(tool_name, arguments),
            risk_description=ApprovalPolicy.risk_description(tool_name, arguments),
            suggestion=suggestion,
            caller_context=caller_context,
            tool_call_id=tool_call_id,
            change_preview=change_preview,
            display_arguments=display_arguments,
        )

    def to_display_text(self, use_icons: bool = True) -> str:
        lines = [
            "[HITL] 需要审批",
            f"工具: {self.tool_name}",
            f"等级: {self.display_danger_level if use_icons else self.danger_level_label}",
            f"风险: {self.risk_description}",
        ]
        if self.suggestion:
            lines.append(f"建议: {self.suggestion}")
        if self.caller_context:
            lines.append(f"来源: {self.caller_context}")
        lines.extend(["参数:", *[f"  {key}: {value}" for key, value in self.argument_rows]])
        return "\n".join(lines)

    @property
    def display_danger_level(self) -> str:
        icon = {"safe": "🟢", "medium": "🟡", "high": "🔴"}.get(
            self.danger_level,
            "",
        )
        return f"{icon} {self.danger_level_label}".strip()

    @property
    def danger_level_label(self) -> str:
        return {
            "safe": "低危",
            "medium": "中危",
            "high": "高危",
        }.get(self.danger_level, self.danger_level)

    @property
    def argument_rows(self) -> list[tuple[str, str]]:
        return _format_arguments(self.arguments)


@dataclass(frozen=True)
class ApprovalResult:
    decision: Decision
    modified_arguments: str | None = None
    reason: str | None = None

    @classmethod
    def approved(cls) -> "ApprovalResult":
        return cls(Decision.APPROVED)

    @classmethod
    def approved_all(cls) -> "ApprovalResult":
        return cls(Decision.APPROVED_ALL)

    @classmethod
    def rejected(cls, reason: str | None = None) -> "ApprovalResult":
        return cls(Decision.REJECTED, reason=reason)

    @classmethod
    def modified(cls, arguments: str) -> "ApprovalResult":
        return cls(Decision.MODIFIED, modified_arguments=arguments)

    @classmethod
    def skipped(cls) -> "ApprovalResult":
        return cls(Decision.SKIPPED)

    @property
    def is_rejected(self) -> bool:
        return self.decision == Decision.REJECTED

    @property
    def is_skipped(self) -> bool:
        return self.decision == Decision.SKIPPED

    def effective_arguments(self, original_arguments: str) -> str:
        if self.decision == Decision.MODIFIED and self.modified_arguments:
            return self.modified_arguments
        return original_arguments


def _format_arguments(arguments: str) -> list[tuple[str, str]]:
    try:
        parsed = json.loads(arguments)
    except json.JSONDecodeError:
        return [("value", _truncate(arguments))]
    if not isinstance(parsed, dict):
        return [("value", _truncate(json.dumps(parsed, ensure_ascii=False)))]
    if not parsed:
        return [("-", "（无）")]
    return [(str(key), _format_value(value)) for key, value in parsed.items()]


def _format_value(value: Any) -> str:
    if isinstance(value, str):
        rendered = json.dumps(value, ensure_ascii=False)
    else:
        rendered = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    return _truncate(rendered)


def _truncate(value: str, limit: int = 120) -> str:
    if len(value) <= limit:
        return value
    return f"{value[:limit]}...（共 {len(value)} 字符）"
