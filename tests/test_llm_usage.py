from stellarcode.llm.types import TokenUsage
from stellarcode.llm.usage import UsageLedger


def test_token_usage_prefers_provider_counts_and_details():
    usage = TokenUsage.from_api(
        {
            "prompt_tokens": 120,
            "completion_tokens": 30,
            "total_tokens": 150,
            "prompt_tokens_details": {"cached_tokens": 40},
            "completion_tokens_details": {"reasoning_tokens": 12},
        },
        messages=[{"role": "user", "content": "hello"}],
        tools=None,
        response_message={"role": "assistant", "content": "hi"},
    )

    assert usage == TokenUsage(
        input_tokens=120,
        output_tokens=30,
        total_tokens=150,
        cached_input_tokens=40,
        reasoning_tokens=12,
        exact=True,
    )


def test_token_usage_falls_back_to_an_explicit_estimate():
    usage = TokenUsage.from_api(
        None,
        messages=[{"role": "user", "content": "hello"}],
        tools=[{"type": "function", "function": {"name": "read_file"}}],
        response_message={"role": "assistant", "content": "hi"},
    )

    assert usage.input_tokens > 0
    assert usage.output_tokens > 0
    assert usage.total_tokens == usage.input_tokens + usage.output_tokens
    assert usage.exact is False


def test_usage_ledger_tracks_call_task_and_conversation_totals():
    ledger = UsageLedger(200_000)

    first = ledger.record(
        TokenUsage(100, 20, 120, cached_input_tokens=10),
        provider="deepseek",
        model="deepseek-v4-flash",
        operation="react",
        task_id="task-1",
    )
    second = ledger.record(
        TokenUsage(140, 25, 165),
        provider="deepseek",
        model="deepseek-v4-flash",
        operation="react",
        task_id="task-1",
    )
    third = ledger.record(
        TokenUsage(80, 10, 90),
        provider="deepseek",
        model="deepseek-v4-flash",
        operation="history-compaction",
        task_id="task-2",
    )

    assert first["task_llm_calls"] == 1
    assert second["task_input_tokens"] == 240
    assert second["conversation_input_tokens"] == 240
    assert third["task_input_tokens"] == 80
    assert third["task_llm_calls"] == 1
    assert third["conversation_input_tokens"] == 320
    assert third["conversation_output_tokens"] == 55
    assert third["conversation_llm_calls"] == 3
    assert ledger.snapshot()["last_context_tokens"] == 80


def test_usage_ledger_calculates_deepseek_flash_cost_from_exact_cache_usage():
    ledger = UsageLedger(1_000_000)

    event = ledger.record(
        TokenUsage(
            1_000_000,
            500_000,
            1_500_000,
            cached_input_tokens=100_000,
        ),
        provider="deepseek",
        model="deepseek-v4-flash",
        operation="react",
        task_id="task-1",
    )

    # 0.9M cache miss * $0.14 + 0.1M hit * $0.0028 + 0.5M output * $0.28.
    assert event["estimated_cost"] == 0.26628
    assert event["conversation_estimated_cost"] == 0.26628
    assert event["currency"] == "USD"
    assert event["cost_estimated"] is True
    assert event["conversation_priced_llm_calls"] == 1


def test_usage_ledger_uses_configured_provider_prices(monkeypatch):
    monkeypatch.setenv("GLM_INPUT_COST_PER_MILLION", "1")
    monkeypatch.setenv("GLM_CACHED_INPUT_COST_PER_MILLION", "0.25")
    monkeypatch.setenv("GLM_OUTPUT_COST_PER_MILLION", "2")
    monkeypatch.setenv("GLM_COST_CURRENCY", "CNY")
    ledger = UsageLedger(200_000)

    event = ledger.record(
        TokenUsage(1_000, 500, 1_500, cached_input_tokens=200),
        provider="glm",
        model="custom-glm",
        operation="react",
        task_id="task-1",
    )

    assert event["estimated_cost"] == 0.00185
    assert event["currency"] == "CNY"
    assert event["cost_source"] == "environment"
