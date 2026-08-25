"""Incrementally assemble OpenAI-compatible SSE deltas into one assistant message."""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable
from typing import Any


DeltaCallback = Callable[[str], None]


def consume_chat_completion_stream(
    lines: Iterable[str | bytes],
    on_delta: DeltaCallback | None = None,
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """Assemble an OpenAI-compatible SSE chat response.

    Only public assistant content is forwarded to ``on_delta``. Provider-specific
    reasoning fields are retained in the final message for history compatibility,
    but are deliberately not exposed as answer text.
    """

    message: dict[str, Any] = {"role": "assistant", "content": ""}
    tool_calls: dict[int, dict[str, Any]] = {}
    usage: dict[str, Any] | None = None
    saw_payload = False

    for raw_line in lines:
        line = _decode_line(raw_line).strip()
        if not line or line.startswith(":"):
            continue
        if not line.startswith("data:"):
            continue
        data_text = line[5:].strip()
        if data_text == "[DONE]":
            break
        try:
            payload = json.loads(data_text)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"LLM stream contained invalid JSON: {data_text[:300]}") from exc
        if not isinstance(payload, dict):
            continue
        saw_payload = True
        raw_usage = payload.get("usage")
        if isinstance(raw_usage, dict):
            usage = raw_usage

        choices = payload.get("choices") or []
        if not choices or not isinstance(choices[0], dict):
            continue
        delta = choices[0].get("delta")
        if not isinstance(delta, dict):
            continue
        role = delta.get("role")
        if isinstance(role, str) and role:
            message["role"] = role

        content = _text_delta(delta.get("content"))
        if content:
            message["content"] = str(message.get("content") or "") + content
            _safe_delta(on_delta, content)

        for key in ("reasoning_content", "reasoning"):
            reasoning = delta.get(key)
            if isinstance(reasoning, str) and reasoning:
                message[key] = str(message.get(key) or "") + reasoning

        raw_tool_calls = delta.get("tool_calls")
        if isinstance(raw_tool_calls, list):
            for fallback_index, fragment in enumerate(raw_tool_calls):
                if not isinstance(fragment, dict):
                    continue
                index = _tool_index(fragment.get("index"), fallback_index)
                target = tool_calls.setdefault(
                    index,
                    {
                        "id": "",
                        "type": "function",
                        "function": {"name": "", "arguments": ""},
                    },
                )
                if fragment.get("id"):
                    target["id"] = str(fragment["id"])
                if fragment.get("type"):
                    target["type"] = str(fragment["type"])
                function = fragment.get("function")
                if isinstance(function, dict):
                    target_function = target["function"]
                    if function.get("name"):
                        target_function["name"] += str(function["name"])
                    if function.get("arguments") is not None:
                        target_function["arguments"] += str(function["arguments"])

    if not saw_payload:
        raise RuntimeError("LLM stream ended without a data payload")
    if tool_calls:
        message["tool_calls"] = [tool_calls[index] for index in sorted(tool_calls)]
        if not message.get("content"):
            message["content"] = None
    return message, usage


def _decode_line(value: str | bytes) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="strict")
    return value


def _text_delta(value: object) -> str:
    if isinstance(value, str):
        return value
    if not isinstance(value, list):
        return ""
    parts: list[str] = []
    for item in value:
        if isinstance(item, str):
            parts.append(item)
        elif isinstance(item, dict) and isinstance(item.get("text"), str):
            parts.append(item["text"])
    return "".join(parts)


def _tool_index(value: object, fallback: int) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return fallback


def _safe_delta(callback: DeltaCallback | None, text: str) -> None:
    if callback is None:
        return
    try:
        callback(text)
    except Exception:
        # Rendering is observational. A UI/event failure must not abort a paid LLM call.
        pass
