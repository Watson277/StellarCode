from __future__ import annotations

from typing import Any, Callable

from stellarcode.llm.types import ChatResult, chat_with_optional_delta


class VisionRoutingClient:
    """Route image-bearing turns to a VLM while retaining the primary text model."""

    def __init__(self, primary: object, vision: object) -> None:
        self.primary = primary
        self.vision = vision
        self.model = str(getattr(primary, "model", type(primary).__name__))
        primary_provider = str(
            getattr(primary, "provider_name", type(primary).__name__.removesuffix("Client").lower())
        )
        vision_provider = str(
            getattr(vision, "provider_name", type(vision).__name__.removesuffix("Client").lower())
        )
        self.provider_name = f"{primary_provider}+{vision_provider}-vlm"

    def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        temperature: float = 0.2,
        on_delta: Callable[[str], None] | None = None,
    ) -> ChatResult | dict[str, Any]:
        delegate = self.vision if messages_have_images(messages) else self.primary
        return chat_with_optional_delta(
            delegate,
            messages,
            tools=tools,
            temperature=temperature,
            on_delta=on_delta,
        )

    def supports_image_input(self) -> bool:
        supports = getattr(self.vision, "supports_image_input", None)
        return bool(supports()) if callable(supports) else True

    def model_for_messages(self, messages: list[dict[str, Any]]) -> str:
        delegate = self.vision if messages_have_images(messages) else self.primary
        selector = getattr(delegate, "model_for_messages", None)
        if callable(selector):
            return str(selector(messages))
        return str(getattr(delegate, "model", type(delegate).__name__))


def messages_have_images(messages: list[dict[str, Any]]) -> bool:
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
