from __future__ import annotations

import os
import re
from dataclasses import dataclass

from stellarcode.llm.types import TokenUsage


@dataclass(frozen=True)
class UsageCost:
    amount: float
    currency: str
    estimated: bool
    source: str


@dataclass(frozen=True)
class TokenRates:
    input_per_million: float
    cached_input_per_million: float
    output_per_million: float
    currency: str
    source: str


# Defaults are intentionally narrow: only exact model names with a public official
# pricing table are included. Environment overrides handle proxies and other providers.
_BUILTIN_RATES: dict[tuple[str, str], TokenRates] = {
    ("deepseek", "deepseek-v4-flash"): TokenRates(
        input_per_million=0.14,
        cached_input_per_million=0.0028,
        output_per_million=0.28,
        currency="USD",
        source="deepseek-official-2026-08",
    ),
    ("deepseek", "deepseek-v4-pro"): TokenRates(
        input_per_million=0.435,
        cached_input_per_million=0.003625,
        output_per_million=0.87,
        currency="USD",
        source="deepseek-official-2026-08",
    ),
}


def resolve_usage_cost(
    usage: TokenUsage,
    *,
    provider: str,
    model: str,
) -> UsageCost | None:
    if usage.reported_cost is not None:
        return UsageCost(
            amount=usage.reported_cost,
            currency=usage.currency or _configured_currency(provider) or "USD",
            estimated=False,
            source="provider-reported",
        )

    rates = _configured_rates(provider) or _builtin_rates(provider, model)
    if rates is None:
        return None
    cached_tokens = min(usage.input_tokens, usage.cached_input_tokens)
    uncached_tokens = max(0, usage.input_tokens - cached_tokens)
    amount = (
        uncached_tokens * rates.input_per_million
        + cached_tokens * rates.cached_input_per_million
        + usage.output_tokens * rates.output_per_million
    ) / 1_000_000
    return UsageCost(
        amount=amount,
        currency=rates.currency,
        estimated=True,
        source=rates.source,
    )


def _builtin_rates(provider: str, model: str) -> TokenRates | None:
    normalized_provider = provider.strip().lower()
    if normalized_provider == "deepseek":
        configured_url = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
        normalized_url = configured_url.strip().lower().rstrip("/")
        if normalized_url not in {
            "https://api.deepseek.com",
            "https://api.deepseek.com/chat/completions",
        }:
            return None
    return _BUILTIN_RATES.get((normalized_provider, model.strip().lower()))


def _configured_rates(provider: str) -> TokenRates | None:
    prefix = re.sub(r"[^A-Z0-9]+", "_", provider.strip().upper()).strip("_")
    input_rate = _environment_float(
        f"{prefix}_INPUT_COST_PER_MILLION" if prefix else "",
        "LLM_INPUT_COST_PER_MILLION",
    )
    cached_rate = _environment_float(
        f"{prefix}_CACHED_INPUT_COST_PER_MILLION" if prefix else "",
        "LLM_CACHED_INPUT_COST_PER_MILLION",
    )
    output_rate = _environment_float(
        f"{prefix}_OUTPUT_COST_PER_MILLION" if prefix else "",
        "LLM_OUTPUT_COST_PER_MILLION",
    )
    if input_rate is None or output_rate is None:
        return None
    return TokenRates(
        input_per_million=input_rate,
        cached_input_per_million=(
            cached_rate if cached_rate is not None else input_rate
        ),
        output_per_million=output_rate,
        currency=_configured_currency(provider) or "USD",
        source="environment",
    )


def _configured_currency(provider: str) -> str:
    prefix = re.sub(r"[^A-Z0-9]+", "_", provider.strip().upper()).strip("_")
    value = (
        os.getenv(f"{prefix}_COST_CURRENCY") if prefix else None
    ) or os.getenv("LLM_COST_CURRENCY")
    return str(value or "").strip().upper()


def _environment_float(*names: str) -> float | None:
    for name in names:
        if not name:
            continue
        raw = os.getenv(name)
        if raw is None or not raw.strip():
            continue
        try:
            value = float(raw)
        except ValueError:
            continue
        if value >= 0:
            return value
    return None
