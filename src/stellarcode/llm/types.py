"""Provider-neutral chat result, usage, streaming, and task-scope primitives."""

from __future__ import annotations

import inspect
import json
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, Callable, Iterator

_LLM_OPERATION: ContextVar[str] = ContextVar("stellarcode_llm_operation", default="agent")
_LLM_SESSION_ID: ContextVar[str] = ContextVar("stellarcode_llm_session_id", default="")
_LLM_TASK_ID: ContextVar[str] = ContextVar("stellarcode_llm_task_id", default="")


@dataclass(frozen=True)
class TokenUsage:
    input_tokens: int
    output_tokens: int
    total_tokens: int
    cached_input_tokens: int = 0
    reasoning_tokens: int = 0
    exact: bool = True
    reported_cost: float | None = None
    currency: str = ""

    @classmethod
    def from_api(
        cls,
        raw: object,
        *,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
        response_message: dict[str, Any],
    ) -> "TokenUsage":
        reported_cost: float | None = None
        currency = ""
        if isinstance(raw, dict):
            reported_cost, currency = _reported_cost(raw)
            input_tokens = _integer(raw.get("prompt_tokens", raw.get("input_tokens")))
            output_tokens = _integer(
                raw.get("completion_tokens", raw.get("output_tokens"))
            )
            total_tokens = _integer(raw.get("total_tokens"))
            prompt_details = raw.get("prompt_tokens_details")
            completion_details = raw.get("completion_tokens_details")
            cached = _integer(raw.get("prompt_cache_hit_tokens"))
            reasoning = 0
            if isinstance(prompt_details, dict):
                cached = max(cached, _integer(prompt_details.get("cached_tokens")))
            if isinstance(completion_details, dict):
                reasoning = _integer(completion_details.get("reasoning_tokens"))
            if input_tokens > 0 or output_tokens > 0 or total_tokens > 0:
                if total_tokens <= 0:
                    total_tokens = input_tokens + output_tokens
                return cls(
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    total_tokens=total_tokens,
                    cached_input_tokens=cached,
                    reasoning_tokens=reasoning,
                    exact=True,
                    reported_cost=reported_cost,
                    currency=currency,
                )
        estimated = cls.estimated(messages, tools, response_message)
        if reported_cost is None:
            return estimated
        return cls(
            input_tokens=estimated.input_tokens,
            output_tokens=estimated.output_tokens,
            total_tokens=estimated.total_tokens,
            exact=False,
            reported_cost=reported_cost,
            currency=currency,
        )

    @classmethod
    def estimated(
        cls,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
        response_message: dict[str, Any],
    ) -> "TokenUsage":
        input_tokens = estimate_request_tokens(messages, tools)
        output_tokens = _estimate_text_tokens(_compact_json(response_message))
        return cls(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            total_tokens=input_tokens + output_tokens,
            exact=False,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "total_tokens": self.total_tokens,
            "cached_input_tokens": self.cached_input_tokens,
            "reasoning_tokens": self.reasoning_tokens,
            "exact": self.exact,
            "reported_cost": self.reported_cost,
            "currency": self.currency,
        }


@dataclass(frozen=True, eq=False)
class ChatResult:
    message: dict[str, Any]
    usage: TokenUsage
    provider: str
    model: str

    def __getitem__(self, key: str) -> Any:
        return self.message[key]

    def get(self, key: str, default: Any = None) -> Any:
        return self.message.get(key, default)

    def __eq__(self, other: object) -> bool:
        if isinstance(other, ChatResult):
            return (
                self.message == other.message
                and self.usage == other.usage
                and self.provider == other.provider
                and self.model == other.model
            )
        if isinstance(other, dict):
            return self.message == other
        return False


def normalize_chat_result(
    value: ChatResult | dict[str, Any],
    *,
    client: object,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None,
) -> ChatResult:
    if isinstance(value, ChatResult):
        return value
    if not isinstance(value, dict):
        raise TypeError(f"chat client returned unsupported result: {type(value).__name__}")
    provider = str(
        getattr(client, "provider_name", type(client).__name__.removesuffix("Client").lower())
    )
    model = _request_model(client, messages)
    return ChatResult(
        message=value,
        usage=TokenUsage.estimated(messages, tools, value),
        provider=provider,
        model=model,
    )


def chat_with_optional_delta(
    client: object,
    messages: list[dict[str, Any]],
    *,
    tools: list[dict[str, Any]] | None,
    temperature: float,
    on_delta: Callable[[str], None] | None,
) -> ChatResult | dict[str, Any]:
    """Call old and streaming-capable chat clients through one compatible boundary."""

    chat = getattr(client, "chat")
    if on_delta is not None and supports_delta_callback(client):
        return chat(
            messages,
            tools=tools,
            temperature=temperature,
            on_delta=on_delta,
        )
    return chat(messages, tools=tools, temperature=temperature)


def supports_delta_callback(client: object) -> bool:
    chat = getattr(client, "chat", None)
    if not callable(chat):
        return False
    try:
        parameters = inspect.signature(chat).parameters.values()
    except (TypeError, ValueError):
        return False
    return any(
        parameter.name == "on_delta" or parameter.kind is inspect.Parameter.VAR_KEYWORD
        for parameter in parameters
    )


def estimate_request_tokens(
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None = None,
) -> int:
    # The provider usage returned after a request is authoritative. This conservative
    # preflight estimate exists only so compression can run before the next request.
    message_tokens = _estimate_text_tokens(_compact_json(messages))
    tool_tokens = _estimate_text_tokens(_compact_json(tools or []))
    framing_tokens = max(8, len(messages) * 4)
    return message_tokens + tool_tokens + framing_tokens


def current_llm_operation() -> str:
    return _LLM_OPERATION.get()


def current_llm_scope() -> tuple[str, str]:
    return _LLM_SESSION_ID.get(), _LLM_TASK_ID.get()


@contextmanager
def llm_operation(name: str) -> Iterator[None]:
    token = _LLM_OPERATION.set(name.strip() or "agent")
    try:
        yield
    finally:
        _LLM_OPERATION.reset(token)


@contextmanager
def llm_runtime_scope(session_id: str, task_id: str) -> Iterator[None]:
    session_token = _LLM_SESSION_ID.set(session_id)
    task_token = _LLM_TASK_ID.set(task_id)
    try:
        yield
    finally:
        _LLM_TASK_ID.reset(task_token)
        _LLM_SESSION_ID.reset(session_token)


def _request_model(client: object, messages: list[dict[str, Any]]) -> str:
    selector = getattr(client, "model_for_messages", None)
    if callable(selector):
        try:
            return str(selector(messages))
        except Exception:
            pass
    return str(getattr(client, "model", type(client).__name__))


def _compact_json(value: object) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)
    except (TypeError, ValueError):
        return str(value)


def _integer(value: object) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _reported_cost(raw: dict[str, Any]) -> tuple[float | None, str]:
    currency = str(raw.get("currency") or "").strip().upper()
    candidates: list[object] = [
        raw.get("total_cost"),
        raw.get("cost"),
        raw.get("estimated_cost"),
    ]
    cost_details = raw.get("cost_details")
    if isinstance(cost_details, dict):
        candidates.extend(
            [cost_details.get("total_cost"), cost_details.get("cost")]
        )
        currency = str(cost_details.get("currency") or currency).strip().upper()
    for candidate in candidates:
        try:
            amount = float(candidate)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            continue
        if amount >= 0:
            return amount, currency
    return None, currency


def _estimate_text_tokens(text: str) -> int:
    if not text:
        return 0
    chinese_chars = sum(1 for char in text if "\u4e00" <= char <= "\u9fff")
    other_chars = len(text) - chinese_chars
    return int((chinese_chars / 1.5) + (other_chars / 4.0) + 0.999)
