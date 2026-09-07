"""Small lock-protected accounting helper for memory/context token budgets."""

from __future__ import annotations

import threading


SHORT_TERM_MEMORY_RATIO = 0.50
COMPRESSION_THRESHOLD_RATIO = 0.80


def proportional_short_term_tokens(context_window: int) -> int:
    """Return the default semantic-memory capacity for one model window."""

    if context_window <= 0:
        raise ValueError("context_window must be greater than 0")
    return max(1, int(context_window * SHORT_TERM_MEMORY_RATIO))


class TokenBudget:
    def __init__(
        self,
        context_window: int = 200_000,
        reserved_for_system: int = 500,
        reserved_for_tools: int = 800,
        reserved_for_response: int = 20_000,
        compression_threshold_ratio: float = COMPRESSION_THRESHOLD_RATIO,
    ) -> None:
        if context_window <= 0:
            raise ValueError("context_window must be greater than 0")
        if not 0 < compression_threshold_ratio < 1:
            raise ValueError("compression_threshold_ratio must be between 0 and 1")
        self.context_window = context_window
        self.reserved_for_system = reserved_for_system
        self.reserved_for_tools = reserved_for_tools
        self.reserved_for_response = reserved_for_response
        self.compression_threshold_ratio = compression_threshold_ratio
        self.total_input_tokens = 0
        self.total_output_tokens = 0
        self.llm_call_count = 0
        self._lock = threading.RLock()

    def available_for_conversation(self) -> int:
        return (
            self.context_window
            - self.reserved_for_system
            - self.reserved_for_tools
            - self.reserved_for_response
        )

    def compression_trigger_tokens(self, capacity_tokens: int) -> int:
        if capacity_tokens <= 0:
            raise ValueError("capacity_tokens must be greater than 0")
        return max(1, int(capacity_tokens * self.compression_threshold_ratio))

    def needs_compression(self, current_tokens: int, *, capacity_tokens: int) -> bool:
        return current_tokens >= self.compression_trigger_tokens(capacity_tokens)

    def record_usage(self, input_tokens: int, output_tokens: int) -> None:
        with self._lock:
            self.total_input_tokens += input_tokens
            self.total_output_tokens += output_tokens
            self.llm_call_count += 1

    def report(self) -> str:
        with self._lock:
            total = self.total_input_tokens + self.total_output_tokens
            average = int(total / self.llm_call_count) if self.llm_call_count else 0
            return (
                f"Token stats: calls={self.llm_call_count}, input={self.total_input_tokens}, "
                f"output={self.total_output_tokens}, average={average}, "
                f"budget={self.context_window}, available={self.available_for_conversation()}"
            )
