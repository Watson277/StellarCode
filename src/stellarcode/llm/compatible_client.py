"""Provider-free OpenAI-compatible chat client used by the Runtime."""

from __future__ import annotations

import time
from typing import Any, Callable

from stellarcode.image import strip_images_for_text_model
from stellarcode.llm.message_history import repair_tool_message_history
from stellarcode.llm.openai_stream import consume_chat_completion_stream
from stellarcode.llm.types import ChatResult, TokenUsage


class CompatibleApiError(RuntimeError):
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
            f"OpenAI-compatible API request failed: HTTP {status_code}, "
            f"model={model}, message={message}"
        )


class OpenAICompatibleClient:
    """Chat Completions client configured only by key, URL, and model name."""

    def __init__(
        self,
        *,
        api_key: str = "",
        model: str,
        base_url: str,
        supports_images: bool = False,
        timeout_seconds: float = 120.0,
        max_retries: int = 2,
        retry_base_seconds: float = 1.0,
        role_name: str = "llm",
    ) -> None:
        self.api_key = api_key.strip()
        self.model = model.strip()
        self.base_url = chat_completions_url(base_url)
        self._supports_images = supports_images
        self.timeout_seconds = timeout_seconds
        self.max_retries = max(0, max_retries)
        self.retry_base_seconds = max(0.0, retry_base_seconds)
        self.provider_name = role_name
        if not self.model:
            raise ValueError("LLM_MODEL_NAME is required.")
        if not self.base_url:
            raise ValueError("LLM_BASE_URL is required.")

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

        input_messages = (
            messages if self._supports_images else strip_images_for_text_model(messages)
        )
        prepared_messages, _ = repair_tool_message_history(input_messages)
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

        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        if on_delta is not None:
            message, raw_usage = self._stream_chat(httpx, payload, headers, on_delta)
        else:
            with httpx.Client(timeout=self.timeout_seconds, http2=False) as client:
                for attempt in range(self.max_retries + 1):
                    response = client.post(self.base_url, headers=headers, json=payload)
                    if not response.is_error:
                        break
                    error = compatible_api_error(response, self.model)
                    if not error.retryable or attempt >= self.max_retries:
                        raise error
                    delay = retry_delay_seconds(response, attempt, self.retry_base_seconds)
                    if delay > 0:
                        time.sleep(delay)
                data = response.json()

            choices = data.get("choices") or []
            if not choices:
                raise RuntimeError(f"LLM response has no choices: {data}")
            message = choices[0].get("message")
            if not isinstance(message, dict):
                raise RuntimeError(f"LLM response has no message: {data}")
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
                    error = compatible_api_error(response, self.model)
                    if not error.retryable or attempt >= self.max_retries:
                        raise error
                    delay = retry_delay_seconds(response, attempt, self.retry_base_seconds)
                attempt += 1
                if delay > 0:
                    time.sleep(delay)

    def supports_image_input(self) -> bool:
        return self._supports_images

    def model_for_messages(self, _messages: list[dict[str, Any]]) -> str:
        return self.model


def chat_completions_url(base_url: str) -> str:
    normalized = base_url.strip().rstrip("/")
    if not normalized:
        return ""
    if normalized.endswith("/chat/completions"):
        return normalized
    return normalized + "/chat/completions"


def compatible_api_error(response: Any, model: str) -> CompatibleApiError:
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
    return CompatibleApiError(
        response.status_code,
        model,
        message,
        retryable=response.status_code == 429 or response.status_code >= 500,
    )


def retry_delay_seconds(response: Any, attempt: int, base_seconds: float) -> float:
    retry_after = str(response.headers.get("Retry-After") or "").strip()
    if retry_after:
        try:
            return min(10.0, max(0.0, float(retry_after)))
        except ValueError:
            pass
    return min(10.0, base_seconds * (2**attempt))
