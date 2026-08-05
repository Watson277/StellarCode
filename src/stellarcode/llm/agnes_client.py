from __future__ import annotations

import os
import time
from typing import Any

from stellarcode.image import strip_images_for_text_model


class AgnesApiError(RuntimeError):
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
            f"Agnes API request failed: HTTP {status_code}, model={model}, "
            f"message={message}"
        )


class AgnesClient:
    """OpenAI-compatible Agnes client with automatic text/vision routing."""

    DEFAULT_BASE_URL = "https://apihub.agnes-ai.com/v1"
    DEFAULT_MODEL = "agnes-2.0-flash"

    def __init__(
        self,
        api_key: str | None = None,
        model: str | None = None,
        base_url: str | None = None,
        timeout_seconds: float = 120.0,
        *,
        vision_model: str | None = None,
        max_retries: int = 2,
        retry_base_seconds: float = 1.0,
    ) -> None:
        self.api_key = api_key or os.getenv("AGNES_API_KEY")
        self.model = model or os.getenv("AGNES_MODEL", self.DEFAULT_MODEL)
        configured_vision_model = vision_model or os.getenv(
            "AGNES_VISION_MODEL",
            self.model,
        )
        self.vision_model = (
            ""
            if configured_vision_model.strip().lower() in {"off", "none", "disabled"}
            else configured_vision_model.strip()
        )
        self.base_url = _chat_completions_url(
            base_url or os.getenv("AGNES_BASE_URL", self.DEFAULT_BASE_URL)
        )
        self.timeout_seconds = timeout_seconds
        self.max_retries = max(0, max_retries)
        self.retry_base_seconds = max(0.0, retry_base_seconds)
        self.provider_name = "agnes"

        if not self.api_key:
            raise ValueError("AGNES_API_KEY is required when LLM_PROVIDER=agnes.")

    def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        temperature: float = 0.2,
    ) -> dict[str, Any]:
        try:
            import httpx
        except ModuleNotFoundError as exc:
            raise RuntimeError("Missing dependency: install httpx with `pip install -e .`.") from exc

        request_model = self.model_for_messages(messages)
        prepared_messages = messages
        if _messages_have_images(messages) and not self.vision_model:
            prepared_messages = strip_images_for_text_model(messages)
        payload: dict[str, Any] = {
            "model": request_model,
            "messages": prepared_messages,
            "temperature": temperature,
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        with httpx.Client(timeout=self.timeout_seconds) as client:
            for attempt in range(self.max_retries + 1):
                response = client.post(self.base_url, headers=headers, json=payload)
                if not response.is_error:
                    break
                error = _agnes_api_error(response, request_model)
                if not error.retryable or attempt >= self.max_retries:
                    raise error
                delay = _retry_delay_seconds(response, attempt, self.retry_base_seconds)
                if delay > 0:
                    time.sleep(delay)
            data = response.json()

        choices = data.get("choices") or []
        if not choices:
            raise RuntimeError(f"Agnes response has no choices: {data}")
        message = choices[0].get("message")
        if not isinstance(message, dict):
            raise RuntimeError(f"Agnes response has no message: {data}")
        return message

    def supports_image_input(self) -> bool:
        return bool(self.vision_model)

    def model_for_messages(self, messages: list[dict[str, Any]]) -> str:
        if _messages_have_images(messages) and self.vision_model:
            return self.vision_model
        return self.model


def _chat_completions_url(base_url: str) -> str:
    normalized = base_url.strip().rstrip("/")
    if normalized.endswith("/chat/completions"):
        return normalized
    return normalized + "/chat/completions"


def _messages_have_images(messages: list[dict[str, Any]]) -> bool:
    for message in messages:
        content = message.get("content")
        if not isinstance(content, list):
            continue
        if any(
            isinstance(part, dict) and part.get("type") == "image_url"
            for part in content
        ):
            return True
    return False


def _agnes_api_error(response: Any, model: str) -> AgnesApiError:
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
    return AgnesApiError(response.status_code, model, message, retryable=retryable)


def _retry_delay_seconds(response: Any, attempt: int, base_seconds: float) -> float:
    retry_after = str(response.headers.get("Retry-After") or "").strip()
    if retry_after:
        try:
            return min(10.0, max(0.0, float(retry_after)))
        except ValueError:
            pass
    return min(10.0, base_seconds * (2**attempt))

