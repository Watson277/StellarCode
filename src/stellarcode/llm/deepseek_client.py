"""DeepSeek provider adapter with request/history compatibility safeguards."""

from __future__ import annotations

import os
import time
from typing import Any, Callable

from stellarcode.image import strip_images_for_text_model
from stellarcode.llm.message_history import repair_tool_message_history
from stellarcode.llm.openai_stream import consume_chat_completion_stream
from stellarcode.llm.types import ChatResult, TokenUsage


class DeepSeekApiError(RuntimeError):
    def __init__(
        self,
        status_code: int,
        model: str,
        message: str,
        *,
        retryable: bool = False,
    ) -> None:
        self.status_code = status_code
        self.model = model
        self.message = message
        self.retryable = retryable
        super().__init__(
            f"DeepSeek API request failed: HTTP {status_code}, model={model}, "
            f"message={message}"
        )


class DeepSeekClient:
    """OpenAI-compatible client for DeepSeek text and reasoning models."""

    DEFAULT_BASE_URL = "https://api.deepseek.com"
    DEFAULT_MODEL = "deepseek-v4-flash"

    def __init__(
        self,
        api_key: str | None = None,
        model: str | None = None,
        base_url: str | None = None,
        timeout_seconds: float = 120.0,
        *,
        max_retries: int = 2,
        retry_base_seconds: float = 1.0,
    ) -> None:
        self.api_key = api_key or os.getenv("DEEPSEEK_API_KEY")
        self.model = model or os.getenv("DEEPSEEK_MODEL", self.DEFAULT_MODEL)
        self.base_url = _chat_completions_url(
            base_url or os.getenv("DEEPSEEK_BASE_URL", self.DEFAULT_BASE_URL)
        )
        self.timeout_seconds = timeout_seconds
        self.max_retries = max(0, max_retries)
        self.retry_base_seconds = max(0.0, retry_base_seconds)
        self.provider_name = "deepseek"

        if not self.api_key:
            raise ValueError(
                "DEEPSEEK_API_KEY is required when LLM_PROVIDER=deepseek."
            )

    def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        temperature: float = 0.2,
        on_delta: Callable[[str], None] | None = None,
    ) -> ChatResult:
        try:
            import httpx
        except ModuleNotFoundError as exc:
            raise RuntimeError(
                "Missing dependency: install httpx with `pip install -e .`."
            ) from exc

        prepared_messages, _ = repair_tool_message_history(
            strip_images_for_text_model(messages)
        )
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": prepared_messages,
            "temperature": temperature,
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"
        if on_delta is not None:
            payload["stream"] = True
            payload["stream_options"] = {"include_usage": True}

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        if on_delta is not None:
            message, raw_usage = self._stream_chat(httpx, payload, headers, on_delta)
        else:
            with httpx.Client(timeout=self.timeout_seconds, http2=False) as client:
                for attempt in range(self.max_retries + 1):
                    response = client.post(self.base_url, headers=headers, json=payload)
                    if not response.is_error:
                        break
                    error = _deepseek_api_error(response, self.model)
                    if not error.retryable or attempt >= self.max_retries:
                        raise error
                    delay = _retry_delay_seconds(response, attempt, self.retry_base_seconds)
                    if delay > 0:
                        time.sleep(delay)
                data = response.json()

            choices = data.get("choices") or []
            if not choices:
                raise RuntimeError(f"DeepSeek response has no choices: {data}")
            message = choices[0].get("message")
            if not isinstance(message, dict):
                raise RuntimeError(f"DeepSeek response has no message: {data}")
            raw_usage = data.get("usage")
        return ChatResult(
            message=message,
            usage=TokenUsage.from_api(
                raw_usage,
                messages=prepared_messages,
                tools=tools,
                response_message=message,
            ),
            provider=self.provider_name,
            model=self.model,
        )

    def _stream_chat(
        self,
        httpx: Any,
        payload: dict[str, Any],
        headers: dict[str, str],
        on_delta: Callable[[str], None],
    ) -> tuple[dict[str, Any], dict[str, Any] | None]:
        attempt = 0
        with httpx.Client(timeout=self.timeout_seconds, http2=False) as client:
            while True:
                with client.stream(
                    "POST",
                    self.base_url,
                    headers=headers,
                    json=payload,
                ) as response:
                    if not response.is_error:
                        return consume_chat_completion_stream(response.iter_lines(), on_delta)
                    response.read()
                    if response.status_code == 400 and "stream_options" in payload:
                        payload.pop("stream_options", None)
                        continue
                    error = _deepseek_api_error(response, self.model)
                    if not error.retryable or attempt >= self.max_retries:
                        raise error
                    delay = _retry_delay_seconds(response, attempt, self.retry_base_seconds)
                attempt += 1
                if delay > 0:
                    time.sleep(delay)

    def supports_image_input(self) -> bool:
        return False

    def model_for_messages(self, _messages: list[dict[str, Any]]) -> str:
        return self.model


def _chat_completions_url(base_url: str) -> str:
    normalized = base_url.strip().rstrip("/")
    if normalized.endswith("/chat/completions"):
        return normalized
    return normalized + "/chat/completions"


def _deepseek_api_error(response: Any, model: str) -> DeepSeekApiError:
    message = ""
    try:
        payload = response.json()
    except Exception:
        payload = None
    if isinstance(payload, dict):
        error = payload.get("error")
        if isinstance(error, dict):
            message = str(error.get("message") or error.get("code") or "")
        elif error:
            message = str(error)
        if not message:
            message = str(payload.get("message") or "")
    if not message:
        message = str(getattr(response, "text", "") or "").strip()[:1000]
    if not message:
        message = getattr(response, "reason_phrase", "") or "unknown API error"
    retryable = response.status_code == 429 or response.status_code >= 500
    return DeepSeekApiError(
        response.status_code,
        model,
        message,
        retryable=retryable,
    )


def _retry_delay_seconds(response: Any, attempt: int, base_seconds: float) -> float:
    retry_after = str(response.headers.get("Retry-After") or "").strip()
    if retry_after:
        try:
            return min(10.0, max(0.0, float(retry_after)))
        except ValueError:
            pass
    return min(10.0, base_seconds * (2**attempt))
