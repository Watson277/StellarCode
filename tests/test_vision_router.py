from __future__ import annotations

from typing import Any

from stellarcode.llm import DeepSeekClient, GLMClient, VisionRoutingClient, create_chat_client


def _image_message() -> dict[str, Any]:
    return {
        "role": "user",
        "content": [
            {"type": "text", "text": "describe"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,QUJDRA=="}},
        ],
    }


class _FakeClient:
    def __init__(self, provider: str, model: str) -> None:
        self.provider_name = provider
        self.model = model
        self.calls: list[list[dict[str, Any]]] = []

    def chat(self, messages, tools=None, temperature=0.2):
        self.calls.append(messages)
        return {"role": "assistant", "content": self.model}

    def supports_image_input(self):
        return self.provider_name == "vision"

    def model_for_messages(self, _messages):
        return self.model


def test_router_uses_primary_for_text_and_vision_client_for_images():
    primary = _FakeClient("text", "text-model")
    vision = _FakeClient("vision", "vision-model")
    client = VisionRoutingClient(primary, vision)

    assert client.chat([{"role": "user", "content": "hello"}])["content"] == "text-model"
    assert client.chat([_image_message()])["content"] == "vision-model"
    assert len(primary.calls) == 1
    assert len(vision.calls) == 1
    assert client.model_for_messages([_image_message()]) == "vision-model"
    assert client.supports_image_input() is True


def test_factory_combines_deepseek_text_with_configured_glm_vision(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "deepseek")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "deepseek-test")
    monkeypatch.setenv("DEEPSEEK_MODEL", "deepseek-text")
    monkeypatch.setenv("VISION_PROVIDER", "glm")
    monkeypatch.setenv("GLM_API_KEY", "glm-test")
    monkeypatch.setenv("GLM_VISION_MODEL", "glm-5v-test")

    client = create_chat_client()

    assert isinstance(client, VisionRoutingClient)
    assert isinstance(client.primary, DeepSeekClient)
    assert isinstance(client.vision, GLMClient)
    assert client.model_for_messages([{"role": "user", "content": "hello"}]) == (
        "deepseek-text"
    )
    assert client.model_for_messages([_image_message()]) == "glm-5v-test"


def test_glm_vision_removes_deepseek_reasoning_fields():
    client = GLMClient(api_key="test", model="glm-5v-test", vision_model="glm-5v-test")
    messages = [
        {"role": "assistant", "content": "prior", "reasoning_content": "private"},
        _image_message(),
    ]

    prepared = client._prepare_messages(messages)

    assert "reasoning_content" not in prepared[0]
    assert "reasoning_content" in messages[0]
