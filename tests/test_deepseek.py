from __future__ import annotations

import httpx
import pytest

from stellarcode.llm import DeepSeekApiError, DeepSeekClient, create_chat_client
from stellarcode.llm.message_history import (
    INTERRUPTED_TOOL_RESULT,
    repair_tool_message_history,
)


def _image_message() -> dict[str, object]:
    return {
        "role": "user",
        "content": [
            {"type": "text", "text": "describe"},
            {
                "type": "image_url",
                "image_url": {"url": "data:image/png;base64,QUJDRA=="},
            },
        ],
    }


def test_deepseek_defaults():
    client = DeepSeekClient(api_key="test")

    assert client.provider_name == "deepseek"
    assert client.model == "deepseek-v4-flash"
    assert client.base_url == "https://api.deepseek.com/chat/completions"
    assert client.supports_image_input() is False


def test_deepseek_chat_preserves_reasoning_and_tools(monkeypatch):
    captured = {}
    assistant = {
        "role": "assistant",
        "content": None,
        "reasoning_content": "I should inspect the file.",
        "tool_calls": [
            {
                "id": "call-1",
                "type": "function",
                "function": {"name": "read_file", "arguments": '{"path":"README.md"}'},
            }
        ],
    }

    class FakeHttpClient:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def post(self, url, **kwargs):
            captured["url"] = url
            captured.update(kwargs)
            return httpx.Response(200, json={"choices": [{"message": assistant}]})

    monkeypatch.setattr(httpx, "Client", lambda **_kwargs: FakeHttpClient())
    client = DeepSeekClient(api_key="deepseek-secret")
    tools = [{"type": "function", "function": {"name": "read_file"}}]

    result = client.chat([_image_message()], tools=tools)

    assert result == assistant
    assert captured["headers"]["Authorization"] == "Bearer deepseek-secret"
    assert captured["json"]["model"] == "deepseek-v4-flash"
    assert captured["json"]["tools"] == tools
    assert "image_url" not in str(captured["json"]["messages"])


def test_deepseek_streams_public_content_and_returns_exact_usage(monkeypatch):
    captured = {}

    class FakeStreamResponse:
        is_error = False

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def iter_lines(self):
            return iter(
                [
                    'data: {"choices":[{"delta":{"reasoning_content":"private"}}]}',
                    'data: {"choices":[{"delta":{"content":"hello "}}]}',
                    'data: {"choices":[{"delta":{"content":"world"}}]}',
                    'data: {"choices":[],"usage":{"prompt_tokens":10,'
                    '"completion_tokens":2,"total_tokens":12}}',
                    "data: [DONE]",
                ]
            )

    class FakeHttpClient:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def stream(self, method, url, **kwargs):
            captured.update({"method": method, "url": url, **kwargs})
            return FakeStreamResponse()

    monkeypatch.setattr(httpx, "Client", lambda **_kwargs: FakeHttpClient())
    client = DeepSeekClient(api_key="test")
    deltas: list[str] = []

    result = client.chat(
        [{"role": "user", "content": "hello"}],
        on_delta=deltas.append,
    )

    assert deltas == ["hello ", "world"]
    assert result.message["content"] == "hello world"
    assert result.message["reasoning_content"] == "private"
    assert result.usage.input_tokens == 10
    assert result.usage.output_tokens == 2
    assert result.usage.exact is True
    assert captured["method"] == "POST"
    assert captured["json"]["stream"] is True
    assert captured["json"]["stream_options"] == {"include_usage": True}


def test_deepseek_reports_api_error(monkeypatch):
    class FakeHttpClient:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def post(self, *_args, **_kwargs):
            return httpx.Response(401, json={"error": {"message": "invalid token"}})

    monkeypatch.setattr(httpx, "Client", lambda **_kwargs: FakeHttpClient())
    client = DeepSeekClient(api_key="bad", max_retries=0)

    with pytest.raises(DeepSeekApiError, match="HTTP 401"):
        client.chat([{"role": "user", "content": "hello"}])


def test_factory_selects_deepseek_from_environment(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "deepseek")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test")
    monkeypatch.setenv("DEEPSEEK_MODEL", "deepseek-v4-flash")
    monkeypatch.setenv("VISION_PROVIDER", "disabled")

    client = create_chat_client()

    assert isinstance(client, DeepSeekClient)
    assert client.model == "deepseek-v4-flash"


def test_history_repair_synthesizes_missing_tool_result_before_next_user():
    messages = [
        {"role": "system", "content": "system"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "call-1",
                    "type": "function",
                    "function": {"name": "execute_command", "arguments": "{}"},
                }
            ],
        },
        {"role": "user", "content": "retry"},
    ]

    repaired, repair_count = repair_tool_message_history(messages)

    assert repair_count == 1
    assert [message["role"] for message in repaired] == [
        "system",
        "assistant",
        "tool",
        "user",
    ]
    assert repaired[2]["tool_call_id"] == "call-1"
    assert repaired[2]["content"] == INTERRUPTED_TOOL_RESULT


def test_history_repair_keeps_complete_parallel_tool_round_unchanged():
    messages = [
        {
            "role": "assistant",
            "tool_calls": [
                {"id": "call-1", "function": {"name": "first"}},
                {"id": "call-2", "function": {"name": "second"}},
            ],
        },
        {"role": "tool", "tool_call_id": "call-1", "content": "one"},
        {"role": "tool", "tool_call_id": "call-2", "content": "two"},
    ]

    repaired, repair_count = repair_tool_message_history(messages)

    assert repair_count == 0
    assert repaired == messages


def test_deepseek_repairs_incomplete_history_before_request(monkeypatch):
    captured = {}

    class FakeHttpClient:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def post(self, _url, **kwargs):
            captured.update(kwargs)
            return httpx.Response(
                200,
                json={"choices": [{"message": {"role": "assistant", "content": "ok"}}]},
            )

    monkeypatch.setattr(httpx, "Client", lambda **_kwargs: FakeHttpClient())
    client = DeepSeekClient(api_key="test")
    messages = [
        {
            "role": "assistant",
            "tool_calls": [
                {"id": "call-1", "function": {"name": "execute_command"}}
            ],
        },
        {"role": "user", "content": "continue"},
    ]

    client.chat(messages)

    sent = captured["json"]["messages"]
    assert [message["role"] for message in sent] == ["assistant", "tool", "user"]
