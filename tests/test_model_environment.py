from __future__ import annotations

from stellarcode.llm.agnes_client import AgnesClient
from stellarcode.llm.deepseek_client import DeepSeekClient
from stellarcode.llm.glm_client import GLMClient
from stellarcode.rag.embedding import EmbeddingClient


MODEL_ENV_NAMES = (
    "LLM_API_KEY",
    "LLM_BASE_URL",
    "LLM_MODEL_NAME",
    "VISION_API_KEY",
    "VISION_BASE_URL",
    "VISION_MODEL_NAME",
    "EMBEDDING_API_KEY",
    "EMBEDDING_BASE_URL",
    "EMBEDDING_MODEL_NAME",
    "EMBEDDING_MODEL",
    "DEEPSEEK_API_KEY",
    "DEEPSEEK_BASE_URL",
    "DEEPSEEK_MODEL",
    "GLM_API_KEY",
    "GLM_BASE_URL",
    "GLM_MODEL",
    "GLM_VISION_API_KEY",
    "GLM_VISION_MODEL",
    "AGNES_API_KEY",
    "AGNES_BASE_URL",
    "AGNES_MODEL",
    "AGNES_VISION_MODEL",
)


def _clear_model_environment(monkeypatch) -> None:
    for name in MODEL_ENV_NAMES:
        monkeypatch.delenv(name, raising=False)


def test_generic_text_configuration_has_precedence(monkeypatch) -> None:
    _clear_model_environment(monkeypatch)
    monkeypatch.setenv("LLM_API_KEY", "generic-key")
    monkeypatch.setenv("LLM_BASE_URL", "https://gateway.example/v1")
    monkeypatch.setenv("LLM_MODEL_NAME", "user-selected-model")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "legacy-key")
    monkeypatch.setenv("DEEPSEEK_MODEL", "legacy-model")

    client = DeepSeekClient()

    assert client.api_key == "generic-key"
    assert client.model == "user-selected-model"
    assert client.base_url == "https://gateway.example/v1/chat/completions"


def test_glm_uses_independent_generic_vision_endpoint(monkeypatch) -> None:
    _clear_model_environment(monkeypatch)
    monkeypatch.setenv("LLM_API_KEY", "text-key")
    monkeypatch.setenv("LLM_BASE_URL", "https://text.example/v1")
    monkeypatch.setenv("LLM_MODEL_NAME", "text-model")
    monkeypatch.setenv("VISION_API_KEY", "vision-key")
    monkeypatch.setenv("VISION_BASE_URL", "https://vision.example/v1")
    monkeypatch.setenv("VISION_MODEL_NAME", "glm-5v-user-model")

    client = GLMClient()

    assert client.api_key == "text-key"
    assert client.model == "text-model"
    assert client.vision_api_key == "vision-key"
    assert client.vision_model == "glm-5v-user-model"
    assert client._request_base_url(client.model) == "https://text.example/v1/chat/completions"
    assert (
        client._request_base_url(client.vision_model)
        == "https://vision.example/v1/chat/completions"
    )


def test_agnes_reads_generic_text_and_vision_configuration(monkeypatch) -> None:
    _clear_model_environment(monkeypatch)
    monkeypatch.setenv("LLM_API_KEY", "text-key")
    monkeypatch.setenv("LLM_BASE_URL", "https://text.example/v1")
    monkeypatch.setenv("LLM_MODEL_NAME", "text-model")
    monkeypatch.setenv("VISION_API_KEY", "vision-key")
    monkeypatch.setenv("VISION_BASE_URL", "https://vision.example/v1")
    monkeypatch.setenv("VISION_MODEL_NAME", "vision-model")

    client = AgnesClient()

    assert client.api_key == "text-key"
    assert client.model == "text-model"
    assert client.base_url == "https://text.example/v1/chat/completions"
    assert client.vision_api_key == "vision-key"
    assert client.vision_model == "vision-model"
    assert client.vision_base_url == "https://vision.example/v1/chat/completions"


def test_embedding_model_name_precedes_legacy_name(monkeypatch) -> None:
    _clear_model_environment(monkeypatch)
    monkeypatch.setenv("EMBEDDING_MODEL_NAME", "generic-embedding")
    monkeypatch.setenv("EMBEDDING_MODEL", "legacy-embedding")

    client = EmbeddingClient()

    assert client.model == "generic-embedding"


def test_legacy_provider_variables_remain_supported(monkeypatch) -> None:
    _clear_model_environment(monkeypatch)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "legacy-key")
    monkeypatch.setenv("DEEPSEEK_BASE_URL", "https://legacy.example/v1")
    monkeypatch.setenv("DEEPSEEK_MODEL", "legacy-model")

    client = DeepSeekClient()

    assert client.api_key == "legacy-key"
    assert client.model == "legacy-model"
    assert client.base_url == "https://legacy.example/v1/chat/completions"
