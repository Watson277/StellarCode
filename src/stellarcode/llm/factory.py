"""Assemble provider-free text and optional vision endpoints."""

from __future__ import annotations

import os

from stellarcode.llm.compatible_client import OpenAICompatibleClient
from stellarcode.llm.environment import first_env
from stellarcode.llm.vision_router import VisionRoutingClient


def create_chat_client() -> OpenAICompatibleClient | VisionRoutingClient:
    text = _text_configuration()
    primary = OpenAICompatibleClient(
        api_key=text["api_key"],
        base_url=text["base_url"],
        model=text["model"],
        role_name="llm",
    )
    vision = _vision_configuration()
    if not vision["base_url"] and not vision["model"]:
        return primary
    if not vision["base_url"] or not vision["model"]:
        raise ValueError(
            "VISION_BASE_URL and VISION_MODEL_NAME must both be configured to enable images."
        )
    vision_client = OpenAICompatibleClient(
        api_key=vision["api_key"],
        base_url=vision["base_url"],
        model=vision["model"],
        supports_images=True,
        role_name="vision",
    )
    return VisionRoutingClient(primary, vision_client)


def _text_configuration() -> dict[str, str]:
    legacy_prefix = _legacy_prefix(os.getenv("LLM_PROVIDER", ""))
    legacy_defaults = {
        "DEEPSEEK": ("https://api.deepseek.com", "deepseek-v4-flash"),
        "GLM": ("https://open.bigmodel.cn/api/coding/paas/v4", "glm-5.1"),
        "AGNES": ("https://apihub.agnes-ai.com/v1", "agnes-2.0-flash"),
    }
    default_url, default_model = legacy_defaults.get(legacy_prefix, ("", ""))
    return {
        "api_key": str(
            first_env("LLM_API_KEY", f"{legacy_prefix}_API_KEY" if legacy_prefix else "")
            or ""
        ),
        "base_url": str(
            first_env(
                "LLM_BASE_URL",
                f"{legacy_prefix}_BASE_URL" if legacy_prefix else "",
                default=default_url,
            )
            or ""
        ),
        "model": str(
            first_env(
                "LLM_MODEL_NAME",
                f"{legacy_prefix}_MODEL" if legacy_prefix else "",
                default=default_model,
            )
            or ""
        ),
    }


def _vision_configuration() -> dict[str, str]:
    legacy_prefix = _legacy_prefix(os.getenv("VISION_PROVIDER", ""))
    legacy_default_urls = {
        "GLM": "https://open.bigmodel.cn/api/paas/v4",
        "AGNES": "https://apihub.agnes-ai.com/v1",
    }
    legacy_model = f"{legacy_prefix}_VISION_MODEL" if legacy_prefix else ""
    legacy_key = f"{legacy_prefix}_VISION_API_KEY" if legacy_prefix else ""
    if legacy_prefix and not first_env(legacy_key):
        legacy_key = f"{legacy_prefix}_API_KEY"
    return {
        "api_key": str(first_env("VISION_API_KEY", legacy_key) or ""),
        "base_url": str(
            first_env(
                "VISION_BASE_URL",
                f"{legacy_prefix}_BASE_URL" if legacy_prefix else "",
                default=legacy_default_urls.get(legacy_prefix, ""),
            )
            or ""
        ),
        "model": str(first_env("VISION_MODEL_NAME", legacy_model) or ""),
    }


def _legacy_prefix(value: str) -> str:
    selected = value.strip().upper().replace("-", "_")
    return selected if selected in {"DEEPSEEK", "GLM", "AGNES"} else ""
