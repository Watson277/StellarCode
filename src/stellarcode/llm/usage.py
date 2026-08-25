"""Thread-safe aggregation of per-request token and cost usage."""

from __future__ import annotations

import threading
from typing import Any

from stellarcode.llm.pricing import resolve_usage_cost
from stellarcode.llm.types import TokenUsage


class UsageLedger:
    """Thread-safe per-conversation provider usage totals."""

    def __init__(self, context_window: int, persisted: object = None) -> None:
        self.context_window = context_window
        self._lock = threading.RLock()
        data = persisted if isinstance(persisted, dict) else {}
        self.input_tokens = _integer(data.get("input_tokens"))
        self.output_tokens = _integer(data.get("output_tokens"))
        self.cached_input_tokens = _integer(data.get("cached_input_tokens"))
        self.reasoning_tokens = _integer(data.get("reasoning_tokens"))
        self.llm_calls = _integer(data.get("llm_calls"))
        self.last_context_tokens = _integer(data.get("last_context_tokens"))
        self.last_input_tokens = _integer(data.get("last_input_tokens"))
        self.last_output_tokens = _integer(data.get("last_output_tokens"))
        self.last_cached_input_tokens = _integer(data.get("last_cached_input_tokens"))
        self.last_reasoning_tokens = _integer(data.get("last_reasoning_tokens"))
        self.last_exact = bool(data.get("last_exact", False))
        self.estimated_cost = _number(data.get("estimated_cost"))
        self.priced_llm_calls = _integer(data.get("priced_llm_calls"))
        self.last_estimated_cost = _optional_number(data.get("last_estimated_cost"))
        self.last_cost_estimated = bool(data.get("last_cost_estimated", True))
        self.cost_currency = str(data.get("cost_currency") or "")
        self.cost_source = str(data.get("cost_source") or "")
        self.provider = str(data.get("provider") or "")
        self.model = str(data.get("model") or "")
        self.operation = str(data.get("operation") or "")
        self._task_id = ""
        self._task_input_tokens = 0
        self._task_output_tokens = 0
        self._task_cached_input_tokens = 0
        self._task_reasoning_tokens = 0
        self._task_llm_calls = 0
        self._task_estimated_cost = 0.0
        self._task_priced_llm_calls = 0

    def record(
        self,
        usage: TokenUsage,
        *,
        provider: str,
        model: str,
        operation: str,
        task_id: str,
    ) -> dict[str, Any]:
        with self._lock:
            if task_id != self._task_id:
                self._task_id = task_id
                self._task_input_tokens = 0
                self._task_output_tokens = 0
                self._task_cached_input_tokens = 0
                self._task_reasoning_tokens = 0
                self._task_llm_calls = 0
                self._task_estimated_cost = 0.0
                self._task_priced_llm_calls = 0
            self.input_tokens += usage.input_tokens
            self.output_tokens += usage.output_tokens
            self.cached_input_tokens += usage.cached_input_tokens
            self.reasoning_tokens += usage.reasoning_tokens
            self.llm_calls += 1
            self._task_input_tokens += usage.input_tokens
            self._task_output_tokens += usage.output_tokens
            self._task_cached_input_tokens += usage.cached_input_tokens
            self._task_reasoning_tokens += usage.reasoning_tokens
            self._task_llm_calls += 1
            self.last_context_tokens = usage.input_tokens
            self.last_input_tokens = usage.input_tokens
            self.last_output_tokens = usage.output_tokens
            self.last_cached_input_tokens = usage.cached_input_tokens
            self.last_reasoning_tokens = usage.reasoning_tokens
            self.last_exact = usage.exact
            self.provider = provider
            self.model = model
            self.operation = operation
            cost = resolve_usage_cost(usage, provider=provider, model=model)
            self.last_estimated_cost = cost.amount if cost is not None else None
            self.last_cost_estimated = cost.estimated if cost is not None else True
            self.cost_source = cost.source if cost is not None else ""
            if cost is not None and (
                not self.cost_currency or self.cost_currency == cost.currency
            ):
                self.cost_currency = cost.currency
                self.estimated_cost += cost.amount
                self.priced_llm_calls += 1
                self._task_estimated_cost += cost.amount
                self._task_priced_llm_calls += 1
            elif cost is not None:
                self.cost_source = "mixed-currency"
            return self._event_payload()

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "input_tokens": self.input_tokens,
                "output_tokens": self.output_tokens,
                "cached_input_tokens": self.cached_input_tokens,
                "reasoning_tokens": self.reasoning_tokens,
                "llm_calls": self.llm_calls,
                "last_context_tokens": self.last_context_tokens,
                "last_input_tokens": self.last_input_tokens,
                "last_output_tokens": self.last_output_tokens,
                "last_cached_input_tokens": self.last_cached_input_tokens,
                "last_reasoning_tokens": self.last_reasoning_tokens,
                "last_exact": self.last_exact,
                "estimated_cost": self.estimated_cost,
                "priced_llm_calls": self.priced_llm_calls,
                "last_estimated_cost": self.last_estimated_cost,
                "last_cost_estimated": self.last_cost_estimated,
                "cost_currency": self.cost_currency,
                "cost_source": self.cost_source,
                "provider": self.provider,
                "model": self.model,
                "operation": self.operation,
                "context_window": self.context_window,
            }

    def _event_payload(self) -> dict[str, Any]:
        return {
            "input_tokens": self.last_input_tokens,
            "output_tokens": self.last_output_tokens,
            "cached_input_tokens": self.last_cached_input_tokens,
            "reasoning_tokens": self.last_reasoning_tokens,
            "context_tokens": self.last_context_tokens,
            "context_window": self.context_window,
            "task_input_tokens": self._task_input_tokens,
            "task_output_tokens": self._task_output_tokens,
            "task_cached_input_tokens": self._task_cached_input_tokens,
            "task_reasoning_tokens": self._task_reasoning_tokens,
            "task_llm_calls": self._task_llm_calls,
            "conversation_input_tokens": self.input_tokens,
            "conversation_output_tokens": self.output_tokens,
            "conversation_cached_input_tokens": self.cached_input_tokens,
            "conversation_reasoning_tokens": self.reasoning_tokens,
            "conversation_llm_calls": self.llm_calls,
            "provider": self.provider,
            "model": self.model,
            "operation": self.operation,
            "exact": self.last_exact,
            "estimated_cost": self.last_estimated_cost,
            "task_estimated_cost": self._task_estimated_cost,
            "conversation_estimated_cost": self.estimated_cost,
            "currency": self.cost_currency,
            "cost_estimated": self.last_cost_estimated,
            "cost_source": self.cost_source,
            "task_priced_llm_calls": self._task_priced_llm_calls,
            "conversation_priced_llm_calls": self.priced_llm_calls,
        }


def _integer(value: object) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _number(value: object) -> float:
    parsed = _optional_number(value)
    return parsed if parsed is not None else 0.0


def _optional_number(value: object) -> float | None:
    try:
        parsed = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return max(0.0, parsed)
