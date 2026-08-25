from __future__ import annotations

import httpx
import pytest

from stellarcode.llm import AgnesApiError, AgnesClient, OpenAICompatibleClient, create_chat_client


def _image_message() -> dict[str, object]:
    return {
        "role": "user",
        "content": [
            {"type": "text", "text": "describe"},
            {
                "type": "image_url",
                "image_url": {"url": "data:image/png;base64,QUJDRA=="},
            },
        ],
    }


def test_agnes_defaults_and_routes_images_to_configured_model():
    client = AgnesClient(api_key="test")
    routed = AgnesClient(
        api_key="test",
        model="agnes-text",
        vision_model="agnes-vision",
        base_url="https://example.test/v1/",
    )

    assert client.model == "agnes-2.0-flash"
    assert client.vision_model == "agnes-2.0-flash"
    assert client.base_url == "https://apihub.agnes-ai.com/v1/chat/completions"
    assert routed.model_for_messages([{"role": "user", "content": "hello"}]) == (
        "agnes-text"
    )
    assert routed.model_for_messages([_image_message()]) == "agnes-vision"
    assert routed.base_url == "https://example.test/v1/chat/completions"


def test_agnes_chat_uses_openai_compatible_tools_and_image_payload(monkeypatch):
    captured = {}

    class FakeHttpClient:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def post(self, url, **kwargs):
            captured["url"] = url
            captured.update(kwargs)
            return httpx.Response(
                200,
                json={"choices": [{"message": {"role": "assistant", "content": "ok"}}]},
            )

    monkeypatch.setattr(httpx, "Client", lambda **_kwargs: FakeHttpClient())
    client = AgnesClient(
        api_key="agnes-secret",
        model="agnes-text",
        vision_model="agnes-vision",
    )
    tools = [{"type": "function", "function": {"name": "read_file"}}]

    result = client.chat([_image_message()], tools=tools)

    assert result["content"] == "ok"
    assert captured["url"].endswith("/v1/chat/completions")
    assert captured["headers"]["Authorization"] == "Bearer agnes-secret"
    assert captured["json"]["model"] == "agnes-vision"
    assert captured["json"]["messages"][0]["content"][1]["type"] == "image_url"
    assert captured["json"]["tools"] == tools
    assert captured["json"]["tool_choice"] == "auto"


def test_agnes_reports_api_error(monkeypatch):
    class FakeHttpClient:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def post(self, *_args, **_kwargs):
            return httpx.Response(
                401,
                json={"error": {"message": "invalid token", "type": "AgnesAI_error"}},
            )

    monkeypatch.setattr(httpx, "Client", lambda **_kwargs: FakeHttpClient())
    client = AgnesClient(api_key="bad", max_retries=0)

    with pytest.raises(AgnesApiError, match="HTTP 401"):
        client.chat([{"role": "user", "content": "hello"}])


def test_factory_accepts_keyless_local_endpoint(monkeypatch):
    monkeypatch.setenv("LLM_API_KEY", "")
    monkeypatch.setenv("LLM_BASE_URL", "http://127.0.0.1:11434/v1")
    monkeypatch.setenv("LLM_MODEL_NAME", "local-model")
    monkeypatch.delenv("VISION_BASE_URL", raising=False)
    monkeypatch.delenv("VISION_MODEL_NAME", raising=False)

    client = create_chat_client()

    assert isinstance(client, OpenAICompatibleClient)
    assert client.api_key == ""
    assert client.model == "local-model"


def test_factory_requires_url_and_model_instead_of_provider(monkeypatch):
    monkeypatch.delenv("LLM_PROVIDER", raising=False)
    monkeypatch.delenv("LLM_BASE_URL", raising=False)
    monkeypatch.delenv("LLM_MODEL_NAME", raising=False)

    with pytest.raises(ValueError, match="LLM_MODEL_NAME is required"):
        create_chat_client()
