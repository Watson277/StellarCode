from __future__ import annotations

import threading


class TokenBudget:
    def __init__(
        self,
        context_window: int = 200_000,
        reserved_for_system: int = 500,
        reserved_for_tools: int = 800,
        reserved_for_response: int = 20_000,
    ) -> None:
        self.context_window = context_window
        self.reserved_for_system = reserved_for_system
        self.reserved_for_tools = reserved_for_tools
        self.reserved_for_response = reserved_for_response
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

    def needs_compression(self, current_tokens: int) -> bool:
        return current_tokens > int(self.available_for_conversation() * 0.8)

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
