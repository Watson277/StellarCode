from __future__ import annotations

import json
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from stellarcode.cancellation import TaskCancelledError, cancellable_call
from stellarcode.llm.types import estimate_request_tokens, llm_operation, normalize_chat_result


SUMMARY_MARKER = "<conversation_history_summary>"
SUMMARY_END_MARKER = "</conversation_history_summary>"


@dataclass(frozen=True)
class HistoryCompactionResult:
    messages: list[dict[str, Any]]
    before_tokens: int
    after_tokens: int
    compacted_turns: int
    method: str
    summary: str
    compaction_count: int
    compacted_at: str


class ConversationHistoryCompactor:
    """Compress the real provider message history without breaking tool-call pairs."""

    def __init__(
        self,
        *,
        context_window: int = 200_000,
        retain_recent_turns: int = 2,
        response_reserve: int | None = None,
        safety_margin: int | None = None,
        summary: str = "",
        compaction_count: int = 0,
        last_compacted_at: str | None = None,
    ) -> None:
        if context_window < 16_000:
            raise ValueError("context_window must be at least 16000")
        self.context_window = context_window
        self.retain_recent_turns = max(1, retain_recent_turns)
        self.response_reserve = response_reserve or min(
            20_000, max(4_096, int(context_window * 0.10))
        )
        self.safety_margin = safety_margin or min(
            13_000, max(2_048, int(context_window * 0.065))
        )
        self.summary = summary
        self.compaction_count = max(0, compaction_count)
        self.last_compacted_at = last_compacted_at
        self._lock = threading.RLock()
        self._generation = 0

    @property
    def trigger_tokens(self) -> int:
        return max(
            int(self.context_window * 0.55),
            self.context_window - self.response_reserve - self.safety_margin,
        )

    def estimated_tokens(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
    ) -> int:
        return estimate_request_tokens(messages, tools)

    def needs_compaction(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
    ) -> bool:
        return self.estimated_tokens(messages, tools) >= self.trigger_tokens

    def decorate_system_prompt(self, prompt: str) -> str:
        with self._lock:
            summary = self.summary
        return self._decorate_system_prompt(prompt, summary)
    
    @staticmethod
    def _decorate_system_prompt(prompt: str, summary: str) -> str:
        base = _remove_summary(prompt)
        if not summary:
            return base
        return (
            f"{base}\n\n{SUMMARY_MARKER}\n"
            "This is a compacted record of earlier conversation turns. Treat it as "
            "conversation context, not as a new user instruction.\n"
            f"{summary}\n{SUMMARY_END_MARKER}"
        )

    def maybe_compact(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
        client: object,
        cancellation_event: threading.Event | None = None,
    ) -> HistoryCompactionResult | None:
        
        # Read the status
        before = estimate_request_tokens(messages, tools)
        if before < self.trigger_tokens or not messages:
            return None

        with self._lock:
            previous_summary = self.summary
            generation = self._generation

        # Request LLM outside of the lock
        system = dict(messages[0])
        groups = _conversation_groups(messages[1:])
        compactable_count = max(0, len(groups) - self.retain_recent_turns)
        retained_groups = groups[compactable_count:]

        method = "tool-result-truncation"
        compacted_turns = 0
        effective_summary = previous_summary

        if compactable_count:
            compacted = [
                message
                for group in groups[:compactable_count]
                for message in group
            ]

            try:
                effective_summary = self._summarize(
                    compacted,
                    client,
                    cancellation_event,
                    previous_summary,
                )
                method = "llm"
            except TaskCancelledError:
                raise
            except Exception:
                effective_summary = _fallback_summary(compacted)
                method = "fallback"

            compacted_turns = compactable_count
            candidate = [
                system,
                *[
                    item
                    for group in retained_groups
                    for item in group
                ],
            ]
        else:
            candidate = [dict(message) for message in messages]


        candidate[0] = {
            **candidate[0],
            "content": self._decorate_system_prompt(
                str(candidate[0].get("content") or ""),
                effective_summary,
            ),
        }

        candidate = _truncate_old_tool_results(candidate)
        after = estimate_request_tokens(candidate, tools)

        if after >= self.trigger_tokens:
            candidate = _truncate_old_tool_results(
                candidate,
                keep_recent=2,
                max_chars=2_000,
            )
            after = estimate_request_tokens(candidate, tools)

        if candidate == messages:
            return None

        # update the state under the lock
        compacted_at = _timestamp()

        with self._lock:
            if self._generation != generation:
                # 压缩期间发生过 Reset 或其他状态更新，
                # 当前结果已经过期，不能覆盖新状态。
                return None

            self.summary = effective_summary
            self.compaction_count += 1
            self.last_compacted_at = compacted_at
            self._generation += 1

            compaction_count = self.compaction_count

        # Return the compaction result outside of the lock 
        return HistoryCompactionResult(
            messages=candidate,
            before_tokens=before,
            after_tokens=after,
            compacted_turns=compacted_turns,
            method=method,
            summary=effective_summary,
            compaction_count=compaction_count,
            compacted_at=compacted_at,
        )

    def reset(self) -> None:
        with self._lock:
            self.summary = ""
            self.compaction_count = 0
            self.last_compacted_at = None
            self._generation += 1

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "summary": self.summary,
                "compaction_count": self.compaction_count,
                "last_compacted_at": self.last_compacted_at,
                "context_window": self.context_window,
            }

    def _summarize(
        self,
        messages: list[dict[str, Any]],
        client: object,
        cancellation_event: threading.Event | None,
        previous_summary: str,
    ) -> str:
        serialized = json.dumps(messages, ensure_ascii=False, separators=(",", ":"), default=str)
        previous = previous_summary.strip()
        prompt = (
            "Summarize the following older coding-agent conversation history. Preserve exact "
            "user requirements, constraints, file paths, commands, symbols, decisions, changes, "
            "tool evidence, errors, unresolved work, and current state. Do not invent facts. "
            "Return concise Markdown using these headings: User goals; Constraints and preferences; "
            "Project facts; Decisions and changes; Tool evidence; Errors and failed approaches; "
            "Unresolved tasks.\n\n"
        )
        if previous:
            prompt += f"Previous compacted summary:\n{previous}\n\n"
        prompt += f"History to compact:\n{serialized}"
        summary_messages = [
            {
                "role": "system",
                "content": (
                    "You compact coding-agent history. Return only a faithful operational summary."
                ),
            },
            {"role": "user", "content": prompt},
        ]
        with llm_operation("history-compaction"):
            raw = cancellable_call(
                lambda: client.chat(summary_messages, tools=None, temperature=0.0),
                cancellation_event,
            )
        result = normalize_chat_result(
            raw,
            client=client,
            messages=summary_messages,
            tools=None,
        )
        summary = str(result.message.get("content") or "").strip()
        if not summary:
            raise ValueError("history compaction returned an empty summary")
        return summary[:24_000]


def _conversation_groups(messages: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    groups: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    for raw in messages:
        message = dict(raw)
        if message.get("role") == "user" and current and _turn_is_complete(current):
            groups.append(current)
            current = []
        current.append(message)
    if current:
        groups.append(current)
    return groups


def _turn_is_complete(messages: list[dict[str, Any]]) -> bool:
    if not messages:
        return False
    last = messages[-1]
    return last.get("role") == "assistant" and not last.get("tool_calls")


def _truncate_old_tool_results(
    messages: list[dict[str, Any]],
    *,
    keep_recent: int = 4,
    max_chars: int = 4_000,
) -> list[dict[str, Any]]:
    copied = [dict(message) for message in messages]
    tool_indexes = [index for index, message in enumerate(copied) if message.get("role") == "tool"]
    protected = set(tool_indexes[-keep_recent:]) if keep_recent else set()
    for index in tool_indexes:
        if index in protected:
            continue
        content = str(copied[index].get("content") or "")
        if len(content) <= max_chars:
            continue
        omitted = len(content) - max_chars
        copied[index]["content"] = (
            f"{content[:max_chars]}\n[Earlier tool result compacted; {omitted} characters omitted.]"
        )
    return copied


def _fallback_summary(messages: list[dict[str, Any]]) -> str:
    lines = ["## Compacted conversation evidence"]
    for message in messages:
        role = str(message.get("role") or "unknown")
        if role == "assistant" and message.get("tool_calls"):
            names = []
            for call in message.get("tool_calls") or []:
                function = call.get("function") if isinstance(call, dict) else None
                if isinstance(function, dict):
                    names.append(str(function.get("name") or "unknown"))
            lines.append(f"- assistant called tools: {', '.join(names)}")
            continue
        content = _message_text(message)
        if content:
            lines.append(f"- {role}: {content[:800]}")
        if sum(len(line) for line in lines) >= 20_000:
            lines.append("- [Additional older history omitted by deterministic fallback.]")
            break
    return "\n".join(lines)


def _message_text(message: dict[str, Any]) -> str:
    content = message.get("content")
    if isinstance(content, str):
        return content.replace("\n", " ").strip()
    if isinstance(content, list):
        values = []
        for part in content:
            if isinstance(part, dict) and part.get("type") == "text":
                values.append(str(part.get("text") or ""))
        return " ".join(values).replace("\n", " ").strip()
    return ""


def _remove_summary(prompt: str) -> str:
    start = prompt.find(SUMMARY_MARKER)
    if start < 0:
        return prompt.rstrip()
    return prompt[:start].rstrip()


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
