from __future__ import annotations

from typing import Any


INTERRUPTED_TOOL_RESULT = (
    "[TOOL_INTERRUPTED] StellarCode was stopped before this tool returned a result. "
    "Treat the operation as unconfirmed and inspect the current state before retrying."
)


def repair_tool_message_history(
    messages: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], int]:
    """Make persisted chat history satisfy the assistant/tool message protocol."""

    repaired: list[dict[str, Any]] = []
    repair_count = 0
    index = 0
    while index < len(messages):
        message = messages[index]
        if not isinstance(message, dict):
            repair_count += 1
            index += 1
            continue

        role = message.get("role")
        raw_calls = message.get("tool_calls")
        if role == "assistant" and isinstance(raw_calls, list) and raw_calls:
            calls: list[dict[str, Any]] = []
            seen_ids: set[str] = set()
            for call in raw_calls:
                if not isinstance(call, dict):
                    repair_count += 1
                    continue
                call_id = str(call.get("id") or "").strip()
                if not call_id or call_id in seen_ids:
                    repair_count += 1
                    continue
                seen_ids.add(call_id)
                calls.append(call)

            assistant = dict(message)
            if calls:
                if len(calls) != len(raw_calls):
                    assistant["tool_calls"] = calls
                repaired.append(assistant)
            else:
                assistant.pop("tool_calls", None)
                if assistant.get("content") not in {None, ""}:
                    repaired.append(assistant)
                repair_count += 1

            index += 1
            received_ids: set[str] = set()
            while index < len(messages):
                candidate = messages[index]
                if not isinstance(candidate, dict) or candidate.get("role") != "tool":
                    break
                tool_call_id = str(candidate.get("tool_call_id") or "").strip()
                if tool_call_id in seen_ids and tool_call_id not in received_ids:
                    repaired.append(dict(candidate))
                    received_ids.add(tool_call_id)
                else:
                    repair_count += 1
                index += 1

            for call in calls:
                call_id = str(call["id"])
                if call_id in received_ids:
                    continue
                function = call.get("function")
                name = str(function.get("name") or "unknown_tool") if isinstance(function, dict) else "unknown_tool"
                repaired.append(
                    {
                        "role": "tool",
                        "tool_call_id": call_id,
                        "name": name,
                        "content": INTERRUPTED_TOOL_RESULT,
                    }
                )
                repair_count += 1
            continue

        if role == "tool":
            # A tool result without an immediately preceding assistant tool call is invalid.
            repair_count += 1
            index += 1
            continue

        repaired.append(dict(message))
        index += 1

    return repaired, repair_count
