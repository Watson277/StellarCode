import json

import pytest

from stellarcode.agent import Agent
from stellarcode.cancellation import TaskCancelledError
from stellarcode.llm.message_history import repair_tool_message_history
from stellarcode.memory.history_compactor import ConversationHistoryCompactor
from stellarcode.prompt import ContextKind, context_kind
from stellarcode.tools import ToolRegistry


class SummaryClient:
    provider_name = "fake"
    model = "summary-model"

    def __init__(self, *, fail: bool = False):
        self.fail = fail
        self.calls = 0
        self.seen_messages = []

    def chat(self, messages, tools=None, temperature=0.0):
        self.calls += 1
        self.seen_messages.append([dict(message) for message in messages])
        if self.fail:
            raise RuntimeError("summary unavailable")
        return {
            "role": "assistant",
            "content": "## User goals\n- Preserve the requested refactor and test evidence.",
        }


def _large_history():
    return [
        {"role": "system", "content": "base system prompt"},
        {"role": "user", "content": "inspect the project " + ("A" * 14_000)},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "call-1",
                    "type": "function",
                    "function": {"name": "read_file", "arguments": '{"path":"a.py"}'},
                }
            ],
        },
        {
            "role": "tool",
            "tool_call_id": "call-1",
            "name": "read_file",
            "content": "tool evidence " + ("B" * 14_000),
        },
        {"role": "assistant", "content": "first turn completed"},
        {"role": "user", "content": "second request " + ("C" * 8_000)},
        {"role": "assistant", "content": "second answer"},
        {"role": "user", "content": "most recent request " + ("D" * 8_000)},
        {"role": "assistant", "content": "most recent answer"},
    ]


def test_compactor_summarizes_old_turns_without_orphaning_tool_messages():
    client = SummaryClient()
    compactor = ConversationHistoryCompactor(context_window=16_000)

    result = compactor.maybe_compact(_large_history(), tools=[], client=client)

    assert result is not None
    assert result.method == "llm"
    assert result.compacted_turns == 1
    assert result.after_tokens < result.before_tokens
    assert client.calls == 1
    assert result.messages[0]["content"] == "base system prompt"
    summary_messages = [
        message
        for message in result.messages
        if context_kind(message) == ContextKind.CONVERSATION_SUMMARY
    ]
    assert len(summary_messages) == 1
    assert summary_messages[0]["role"] == "user"
    summary_payload = json.loads(summary_messages[0]["content"].splitlines()[-1])
    assert summary_payload["schema"] == "stellarcode.context/v1"
    assert summary_payload["kind"] == "conversation_summary"
    assert summary_payload["trusted"] is False
    assert "Preserve the requested refactor" in summary_payload["content"]
    assert result.messages[-2]["content"].startswith("most recent request")
    assert all(message.get("tool_call_id") != "call-1" for message in result.messages)
    repaired, repair_count = repair_tool_message_history(result.messages)
    assert repaired == result.messages
    assert repair_count == 0


def test_compactor_has_a_deterministic_fallback_when_summary_request_fails():
    compactor = ConversationHistoryCompactor(context_window=16_000)

    result = compactor.maybe_compact(
        _large_history(),
        tools=[],
        client=SummaryClient(fail=True),
    )

    assert result is not None
    assert result.method == "fallback"
    assert "Compacted conversation evidence" in result.summary
    assert result.compaction_count == 1


def test_summary_context_sync_is_idempotent_and_migrates_legacy_system_content():
    compactor = ConversationHistoryCompactor(
        context_window=16_000,
        summary="stable historical facts",
    )
    messages = [
        {
            "role": "system",
            "content": (
                "base system prompt\n\n<conversation_history_summary>\n"
                "legacy summary\n</conversation_history_summary>"
            ),
        },
        {"role": "user", "content": "current request"},
    ]

    once = compactor.sync_summary_context(messages)
    twice = compactor.sync_summary_context(once)

    assert once == twice
    assert once[0] == {"role": "system", "content": "base system prompt"}
    assert context_kind(once[1]) == ContextKind.CONVERSATION_SUMMARY
    assert once[2] == {"role": "user", "content": "current request"}


def test_restored_summary_is_user_context_and_private_metadata_is_not_sent():
    client = SummaryClient()
    agent = Agent(
        client,
        ToolRegistry(),
        history_summary="the project already completed migration step one",
    )

    agent.run("continue with step two")

    assert "migration step one" not in client.seen_messages[-1][0]["content"]
    provider_summary = json.loads(client.seen_messages[-1][1]["content"].splitlines()[-1])
    assert provider_summary["kind"] == "conversation_summary"
    assert "migration step one" in provider_summary["content"]
    assert all(
        not any(key.startswith("_stellarcode_") for key in message)
        for message in client.seen_messages[-1]
    )
    assert context_kind(agent.messages[1]) == ContextKind.CONVERSATION_SUMMARY


def test_agent_emits_visible_compaction_lifecycle_events():
    events = []
    agent = Agent(
        SummaryClient(),
        ToolRegistry(),
        context_window=16_000,
        event_callback=lambda event_type, data: events.append((event_type, data)),
    )
    agent.messages = _large_history()

    agent._chat([], None)

    assert [event_type for event_type, _ in events] == [
        "history.compaction.started",
        "history.compacted",
        "history.compaction.finished",
    ]
    assert events[-1][1] == {"compacted": True}


def test_agent_clears_compaction_status_when_summary_is_cancelled():
    class CancelledSummaryClient(SummaryClient):
        def chat(self, messages, tools=None, temperature=0.0):
            raise TaskCancelledError("Task cancelled by user.")

    events = []
    agent = Agent(
        CancelledSummaryClient(),
        ToolRegistry(),
        context_window=16_000,
        event_callback=lambda event_type, data: events.append((event_type, data)),
    )
    agent.messages = _large_history()

    with pytest.raises(TaskCancelledError):
        agent._chat([], None)

    assert events[0][0] == "history.compaction.started"
    assert events[-1] == ("history.compaction.finished", {"compacted": False})
