"""Create the configured text/vision provider without leaking provider details upward."""

from __future__ import annotations

import os

from stellarcode.llm.agnes_client import AgnesClient
from stellarcode.llm.deepseek_client import DeepSeekClient
from stellarcode.llm.glm_client import GLMClient
from stellarcode.llm.vision_router import VisionRoutingClient


def create_chat_client(
    provider: str | None = None,
) -> AgnesClient | DeepSeekClient | GLMClient | VisionRoutingClient:
    selected = (provider or os.getenv("LLM_PROVIDER", "glm")).strip().lower()
    aliases = {
        "agnes-ai": "agnes",
        "agnes_ai": "agnes",
        "sapiens": "agnes",
        "sapiens-ai": "agnes",
    }
    selected = aliases.get(selected, selected)
    if selected == "agnes":
        return AgnesClient()
    if selected == "deepseek":
        return _with_external_vision_routing(DeepSeekClient())
    if selected == "glm":
        client = GLMClient()
        client.provider_name = "glm"
        return client
    raise ValueError(
        f"Unsupported LLM_PROVIDER: {selected}. Supported providers: glm, agnes, deepseek."
    )


def _with_external_vision_routing(primary: object) -> object:
    selected = os.getenv("VISION_PROVIDER", "auto").strip().lower()
    aliases = {"zhipu": "glm", "bigmodel": "glm", "agnes-ai": "agnes"}
    selected = aliases.get(selected, selected)
    if selected in {"", "off", "none", "disabled"}:
        return primary
    if selected == "auto":
        if _vision_enabled("GLM_API_KEY", "GLM_VISION_MODEL"):
            selected = "glm"
        elif _vision_enabled("AGNES_API_KEY", "AGNES_VISION_MODEL"):
            selected = "agnes"
        else:
            return primary
    if selected == "glm":
        model = os.getenv("GLM_VISION_MODEL", GLMClient.DEFAULT_VISION_MODEL).strip()
        if _disabled(model):
            return primary
        vision = GLMClient(model=model, vision_model=model)
        vision.provider_name = "glm"
        return VisionRoutingClient(primary, vision)
    if selected == "agnes":
        model = os.getenv("AGNES_VISION_MODEL", AgnesClient.DEFAULT_MODEL).strip()
        if _disabled(model):
            return primary
        return VisionRoutingClient(
            primary,
            AgnesClient(model=model, vision_model=model),
        )
    raise ValueError(
        f"Unsupported VISION_PROVIDER: {selected}. Supported providers: auto, glm, agnes."
    )


def _vision_enabled(api_key_name: str, model_name: str) -> bool:
    return bool(os.getenv(api_key_name)) and not _disabled(os.getenv(model_name, ""))


def _disabled(value: str) -> bool:
    return value.strip().lower() in {"off", "none", "disabled"}
