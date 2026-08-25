"""Coordinator, role, and messaging primitives for Team execution mode."""

from stellarcode.multi_agent.message import AgentMessage, MessageType
from stellarcode.multi_agent.message_bus import (
    BusMessage,
    ClaimedMessage,
    FileMessageBus,
    MessageBusError,
)
from stellarcode.multi_agent.orchestrator import (
    AgentOrchestrator,
    MultiAgentError,
    StepExecutionResult,
)
from stellarcode.multi_agent.role import AgentRole
from stellarcode.multi_agent.sub_agent import SubAgent

__all__ = [
    "AgentMessage",
    "AgentOrchestrator",
    "AgentRole",
    "BusMessage",
    "ClaimedMessage",
    "FileMessageBus",
    "MessageType",
    "MessageBusError",
    "MultiAgentError",
    "StepExecutionResult",
    "SubAgent",
]
