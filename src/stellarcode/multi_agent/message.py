"""Structured messages exchanged between coordinator and sub-agent roles."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from stellarcode.multi_agent.role import AgentRole


class MessageType(str, Enum):
    TASK = "TASK"
    RESULT = "RESULT"
    FEEDBACK = "FEEDBACK"
    APPROVAL = "APPROVAL"
    REJECTION = "REJECTION"
    ERROR = "ERROR"


@dataclass(frozen=True)
class AgentMessage:
    from_agent: str
    from_role: AgentRole | None
    content: str
    type: MessageType

    @classmethod
    def task(cls, from_agent: str, content: str) -> "AgentMessage":
        return cls(from_agent, None, content, MessageType.TASK)

    @classmethod
    def result(
        cls,
        from_agent: str,
        role: AgentRole,
        content: str,
    ) -> "AgentMessage":
        return cls(from_agent, role, content, MessageType.RESULT)

    @classmethod
    def feedback(cls, from_agent: str, content: str) -> "AgentMessage":
        return cls(from_agent, AgentRole.REVIEWER, content, MessageType.FEEDBACK)

    @classmethod
    def approval(cls, from_agent: str, content: str) -> "AgentMessage":
        return cls(from_agent, AgentRole.REVIEWER, content, MessageType.APPROVAL)

    @classmethod
    def rejection(cls, from_agent: str, content: str) -> "AgentMessage":
        return cls(from_agent, AgentRole.REVIEWER, content, MessageType.REJECTION)

    @classmethod
    def error(
        cls,
        from_agent: str,
        role: AgentRole,
        content: str,
    ) -> "AgentMessage":
        return cls(from_agent, role, content, MessageType.ERROR)
