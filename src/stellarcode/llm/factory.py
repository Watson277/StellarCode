from __future__ import annotations

import os

from stellarcode.llm.agnes_client import AgnesClient
from stellarcode.llm.glm_client import GLMClient


def create_chat_client(provider: str | None = None) -> AgnesClient | GLMClient:
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
    if selected == "glm":
        client = GLMClient()
        client.provider_name = "glm"
        return client
    raise ValueError(
        f"Unsupported LLM_PROVIDER: {selected}. Supported providers: glm, agnes."
    )
