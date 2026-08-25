from __future__ import annotations

from typing import Any

import httpx
import pytest

from stellarcode.llm import OpenAICompatibleClient, VisionRoutingClient, create_chat_client


class FakeResponse:
    is_error = False

    def json(self) -> dict[str, Any]:
        return {
            "choices": [{"message": {"role": "assistant", "content": "ok"}}],
            "usage": {"prompt_tokens": 4, "completion_tokens": 1, "total_tokens": 5},
        }


class FakeHttpClient:
    def __init__(self, calls: list[dict[str, Any]], **_kwargs: Any) -> None:
        self.calls = calls

    def __enter__(self) -> "FakeHttpClient":
        return self

    def __exit__(self, *_args: Any) -> None:
        return None

    def post(self, url: str, **kwargs: Any) -> FakeResponse:
        self.calls.append({"url": url, **kwargs})
        return FakeResponse()


def test_keyless_compatible_client_omits_authorization_and_preserves_images(
    monkeypatch,
) -> None:
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(httpx, "Client", lambda **kwargs: FakeHttpClient(calls, **kwargs))
    client = OpenAICompatibleClient(
        model="local-vlm",
        base_url="http://127.0.0.1:11434/v1",
        supports_images=True,
        role_name="vision",
    )
    messages = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "describe"},
                {"type": "image_url", "image_url": {"url": "data:image/png;base64,QQ=="}},
            ],
        }
    ]

    result = client.chat(messages)

    assert result.message["content"] == "ok"
    assert calls[0]["url"] == "http://127.0.0.1:11434/v1/chat/completions"
    assert "Authorization" not in calls[0]["headers"]
    assert calls[0]["json"]["messages"] == messages


def test_factory_routes_images_without_a_provider_selector(monkeypatch) -> None:
    monkeypatch.setenv("LLM_API_KEY", "text-key")
    monkeypatch.setenv("LLM_BASE_URL", "https://text.example/v1")
    monkeypatch.setenv("LLM_MODEL_NAME", "text-model")
    monkeypatch.setenv("VISION_API_KEY", "vision-key")
    monkeypatch.setenv("VISION_BASE_URL", "https://vision.example/v1")
    monkeypatch.setenv("VISION_MODEL_NAME", "vision-model")
    for name in ("LLM_PROVIDER", "VISION_PROVIDER", "EMBEDDING_PROVIDER"):
        monkeypatch.delenv(name, raising=False)

    client = create_chat_client()

    assert isinstance(client, VisionRoutingClient)
    assert client.primary.model == "text-model"
    assert client.vision.model == "vision-model"


def test_partial_vision_configuration_is_rejected(monkeypatch) -> None:
    monkeypatch.setenv("LLM_BASE_URL", "https://text.example/v1")
    monkeypatch.setenv("LLM_MODEL_NAME", "text-model")
    monkeypatch.setenv("VISION_MODEL_NAME", "vision-model")
    monkeypatch.delenv("VISION_BASE_URL", raising=False)
    monkeypatch.delenv("VISION_PROVIDER", raising=False)

    with pytest.raises(ValueError, match="VISION_BASE_URL and VISION_MODEL_NAME"):
        create_chat_client()
