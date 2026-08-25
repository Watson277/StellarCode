"""GLM provider adapter for chat, streaming deltas, and tool-call responses."""

from __future__ import annotations

import copy
import os
import time
from typing import Any, Callable

from stellarcode.image import strip_images_for_text_model
from stellarcode.llm.openai_stream import consume_chat_completion_stream
from stellarcode.llm.types import ChatResult, TokenUsage


class GLMApiError(RuntimeError):
    def __init__(
        self,
        status_code: int,
        model: str,
        code: str,
        message: str,
        hint: str,
        *,
        retryable: bool = False,
    ) -> None:
        self.status_code = status_code
        self.model = model
        self.code = code
        self.message = message
        self.hint = hint
        self.retryable = retryable
        code_text = f", code={code}" if code else ""
        hint_text = f" Hint: {hint}" if hint else ""
        super().__init__(
            f"GLM API request failed: HTTP {status_code}, model={model}{code_text}, "
            f"message={message}.{hint_text}"
        )


class GLMClient:
    """Small OpenAI-compatible chat client for GLM models."""

    CODING_API_URL = "https://open.bigmodel.cn/api/coding/paas/v4/chat/completions"
    MULTIMODAL_API_URL = "https://open.bigmodel.cn/api/paas/v4/chat/completions"
    DEFAULT_VISION_MODEL = "glm-5v-turbo"
    VISION_MODEL_PREFIXES = ("glm-5v", "glm-4.6v", "glm-4.5v", "glm-4.1v", "glm-4v")

    def __init__(
        self,
        api_key: str | None = None,
        model: str | None = None,
        base_url: str | None = None,
        timeout_seconds: float = 120.0,
        *,
        vision_model: str | None = None,
        vision_api_key: str | None = None,
        max_retries: int = 2,
        retry_base_seconds: float = 1.0,
    ) -> None:
        self.api_key = api_key or os.getenv("GLM_API_KEY")
        self.model = model or os.getenv("GLM_MODEL", "glm-5.1")
        configured_vision_model = vision_model or os.getenv(
            "GLM_VISION_MODEL",
            self.DEFAULT_VISION_MODEL,
        )
        self.vision_model = (
            ""
            if configured_vision_model.strip().lower() in {"off", "none", "disabled"}
            else configured_vision_model.strip()
        )
        self.vision_api_key = vision_api_key or os.getenv("GLM_VISION_API_KEY") or self.api_key
        configured_url = base_url or os.getenv("GLM_BASE_URL")
        self._configured_base_url = configured_url
        self.base_url = configured_url or self._default_base_url(self.model)
        self.timeout_seconds = timeout_seconds
        self.max_retries = max(0, max_retries)
        self.retry_base_seconds = max(0.0, retry_base_seconds)

        if not self.api_key:
            raise ValueError("GLM_API_KEY is required. Put it in .env or your environment.")

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
            raise RuntimeError("Missing dependency: install httpx with `pip install -e .`.") from exc

        request_model = self.model_for_messages(messages)
        prepared_messages = self._prepare_messages(messages, request_model)
        payload: dict[str, Any] = {
            "model": request_model,
            "messages": prepared_messages,
            "temperature": temperature,
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"
        if on_delta is not None:
            payload["stream"] = True
            payload["stream_options"] = {"include_usage": True}

        request_api_key = (
            self.vision_api_key if self._is_vision_model(request_model) else self.api_key
        )
        headers = {
            "Authorization": f"Bearer {request_api_key}",
            "Content-Type": "application/json",
        }

        if on_delta is not None:
            message, raw_usage = self._stream_chat(
                httpx,
                payload,
                headers,
                request_model,
                on_delta,
            )
        else:
            with httpx.Client(timeout=self.timeout_seconds) as client:
                for attempt in range(self.max_retries + 1):
                    response = client.post(
                        self._request_base_url(request_model),
                        headers=headers,
                        json=payload,
                    )
                    if not response.is_error:
                        break
                    error = _glm_api_error(response, request_model)
                    if not error.retryable or attempt >= self.max_retries:
                        raise error
                    delay = _retry_delay_seconds(
                        response,
                        attempt,
                        self.retry_base_seconds,
                    )
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
            provider=str(getattr(self, "provider_name", "glm")),
            model=request_model,
        )

    def _stream_chat(
        self,
        httpx: Any,
        payload: dict[str, Any],
        headers: dict[str, str],
        request_model: str,
        on_delta: Callable[[str], None],
    ) -> tuple[dict[str, Any], dict[str, Any] | None]:
        attempt = 0
        with httpx.Client(timeout=self.timeout_seconds) as client:
            while True:
                with client.stream(
                    "POST",
                    self._request_base_url(request_model),
                    headers=headers,
                    json=payload,
                ) as response:
                    if not response.is_error:
                        return consume_chat_completion_stream(response.iter_lines(), on_delta)
                    response.read()
                    if response.status_code == 400 and "stream_options" in payload:
                        # Some OpenAI-compatible gateways stream correctly but do not
                        # implement the optional final usage chunk.
                        payload.pop("stream_options", None)
                        continue
                    error = _glm_api_error(response, request_model)
                    if not error.retryable or attempt >= self.max_retries:
                        raise error
                    delay = _retry_delay_seconds(response, attempt, self.retry_base_seconds)
                attempt += 1
                if delay > 0:
                    time.sleep(delay)

    def supports_image_input(self) -> bool:
        return self._is_vision_model(self.model) or bool(self.vision_model)

    def model_for_messages(self, messages: list[dict[str, Any]]) -> str:
        if _messages_have_images(messages) and self.vision_model:
            return self.vision_model
        return self.model

    def _default_base_url(self, model: str) -> str:
        return self.MULTIMODAL_API_URL if self._is_vision_model(model) else self.CODING_API_URL

    def _request_base_url(self, model: str) -> str:
        return self._configured_base_url or self._default_base_url(model)

    def _is_vision_model(self, model: str) -> bool:
        return model.strip().lower().startswith(self.VISION_MODEL_PREFIXES)

    def _prepare_messages(
        self,
        messages: list[dict[str, Any]],
        request_model: str | None = None,
    ) -> list[dict[str, Any]]:
        prepared = copy.deepcopy(messages)
        model = request_model or self.model_for_messages(messages)
        if not self._is_vision_model(model):
            return strip_images_for_text_model(prepared)
        for message in prepared:
            # DeepSeek reasoning fields are provider-specific and can be present when a
            # text conversation routes a later image turn to GLM.
            message.pop("reasoning_content", None)
        return prepared


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


def _glm_api_error(response: Any, model: str) -> GLMApiError:
    payload: object = None
    try:
        payload = response.json()
    except Exception:
        pass

    code = ""
    message = ""
    if isinstance(payload, dict):
        error = payload.get("error")
        if isinstance(error, dict):
            code = str(error.get("code") or "")
            message = str(error.get("message") or "")
        else:
            code = str(payload.get("code") or "")
            message = str(payload.get("message") or "")
    if not message:
        message = str(getattr(response, "text", "") or "").strip()[:1000]
    if not message:
        message = getattr(response, "reason_phrase", "") or "unknown API error"

    hints = {
        "1113": "账户欠费，请检查开放平台余额",
        "1302": "账户达到速率限制；StellarCode 已进行短暂退避重试",
        "1304": "已达到该 API 今日调用次数上限",
        "1305": "模型当前访问量过大；StellarCode 已进行短暂退避重试",
        "1308": "已达到周期使用上限，请等待响应中给出的重置时间",
        "1309": "GLM Coding Plan 已到期",
        "1310": "已达到每周或每月使用上限",
        "1311": "当前套餐未开放该视觉模型权限，请更换模型或套餐",
        "1315": "当前 API Key 仅限企业编程套餐，标准 API 需要对应类型的 Key",
    }
    hint = hints.get(code, "")
    if response.status_code == 429 and not hint:
        hint = "请依据业务错误码检查限流、账户余额和模型权限"
    return GLMApiError(
        response.status_code,
        model,
        code,
        message,
        hint,
        retryable=code in {"1302", "1305"},
    )


def _retry_delay_seconds(response: Any, attempt: int, base_seconds: float) -> float:
    retry_after = str(response.headers.get("Retry-After") or "").strip()
    if retry_after:
        try:
            return min(10.0, max(0.0, float(retry_after)))
        except ValueError:
            pass
    return min(10.0, base_seconds * (2**attempt))
