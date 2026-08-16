from __future__ import annotations

import json

from stellarcode.llm.openai_stream import consume_chat_completion_stream


def _event(payload: dict[str, object]) -> str:
    return f"data: {json.dumps(payload)}"


def test_stream_assembles_content_reasoning_tools_and_usage():
    deltas: list[str] = []
    lines = [
        _event(
            {
                "choices": [
                    {
                        "delta": {
                            "role": "assistant",
                            "reasoning_content": "private ",
                        }
                    }
                ]
            }
        ),
        _event({"choices": [{"delta": {"content": "Hello "}}]}),
        _event({"choices": [{"delta": {"content": "world"}}]}),
        _event(
            {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "call-1",
                                    "type": "function",
                                    "function": {
                                        "name": "read_",
                                        "arguments": '{"path":',
                                    },
                                }
                            ]
                        }
                    }
                ]
            }
        ),
        _event(
            {
                "choices": [
                    {
                        "delta": {
                            "reasoning_content": "reasoning",
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "function": {
                                        "name": "file",
                                        "arguments": '"README.md"}',
                                    },
                                }
                            ],
                        }
                    }
                ]
            }
        ),
        _event(
            {
                "choices": [],
                "usage": {
                    "prompt_tokens": 12,
                    "completion_tokens": 4,
                    "total_tokens": 16,
                },
            }
        ),
        "data: [DONE]",
    ]

    message, usage = consume_chat_completion_stream(lines, deltas.append)

    assert deltas == ["Hello ", "world"]
    assert message["content"] == "Hello world"
    assert message["reasoning_content"] == "private reasoning"
    assert message["tool_calls"] == [
        {
            "id": "call-1",
            "type": "function",
            "function": {
                "name": "read_file",
                "arguments": '{"path":"README.md"}',
            },
        }
    ]
    assert usage == {
        "prompt_tokens": 12,
        "completion_tokens": 4,
        "total_tokens": 16,
    }

