"""Embedding provider adapter with deterministic offline fallback for local RAG."""

from __future__ import annotations

import hashlib
import math
import os
import re
from typing import Any


class EmbeddingError(RuntimeError):
    pass


class EmbeddingClient:
    MAX_INPUT_CHARS = 2000

    def __init__(
        self,
        provider: str | None = None,
        model: str | None = None,
        base_url: str | None = None,
        api_key: str | None = None,
        timeout_seconds: float = 120.0,
    ) -> None:
        self.provider = (provider or os.getenv("EMBEDDING_PROVIDER", "ollama")).lower()
        self.model = model or os.getenv("EMBEDDING_MODEL") or _default_model(self.provider)
        self.base_url = (
            base_url
            or os.getenv("EMBEDDING_BASE_URL")
            or _default_base_url(self.provider)
        ).rstrip("/")
        self.api_key = api_key if api_key is not None else os.getenv("EMBEDDING_API_KEY", "")
        self.timeout_seconds = timeout_seconds

    def embed(self, text: str | None) -> list[float]:
        if not text:
            return []
        content = text[: self.MAX_INPUT_CHARS]
        if self.provider == "local":
            return _local_hash_embedding(content)
        if self.provider == "ollama":
            return self._embed_ollama(content)
        if self.provider in {"openai", "zhipu", "glm"}:
            return self._embed_openai_compatible(content)
        raise EmbeddingError(f"Unsupported embedding provider: {self.provider}")

    def _embed_ollama(self, text: str) -> list[float]:
        data = self._post_json(
            f"{self.base_url}/api/embeddings",
            {"model": self.model, "prompt": text},
            use_auth=False,
        )
        embedding = data.get("embedding")
        return _parse_embedding(embedding, "Ollama")

    def _embed_openai_compatible(self, text: str) -> list[float]:
        data = self._post_json(
            f"{self.base_url}/embeddings",
            {"model": self.model, "input": text},
            use_auth=True,
        )
        entries = data.get("data")
        if not isinstance(entries, list) or not entries or not isinstance(entries[0], dict):
            raise EmbeddingError(f"Embedding API returned an invalid data field: {data}")
        return _parse_embedding(entries[0].get("embedding"), "Embedding API")

    def _post_json(
        self,
        url: str,
        payload: dict[str, Any],
        use_auth: bool,
    ) -> dict[str, Any]:
        try:
            import httpx
        except ModuleNotFoundError as exc:
            raise EmbeddingError("Missing dependency: httpx") from exc

        headers = {"Content-Type": "application/json"}
        if use_auth and self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        timeout = httpx.Timeout(self.timeout_seconds, connect=30.0)
        try:
            with httpx.Client(timeout=timeout) as client:
                response = client.post(url, headers=headers, json=payload)
                response.raise_for_status()
                data = response.json()
        except Exception as exc:
            raise EmbeddingError(f"Embedding request failed for {url}: {exc}") from exc
        if not isinstance(data, dict):
            raise EmbeddingError(f"Embedding API returned a non-object response: {data}")
        return data


def _default_model(provider: str) -> str:
    if provider == "local":
        return "hash-embedding-256"
    if provider in {"zhipu", "glm"}:
        return "embedding-3"
    if provider == "openai":
        return "text-embedding-3-small"
    return "nomic-embed-text:latest"


def _default_base_url(provider: str) -> str:
    if provider in {"zhipu", "glm"}:
        return "https://open.bigmodel.cn/api/paas/v4"
    if provider == "openai":
        return "https://api.openai.com/v1"
    return "http://localhost:11434"


def _parse_embedding(value: object, provider_name: str) -> list[float]:
    if not isinstance(value, list) or not value:
        raise EmbeddingError(f"{provider_name} returned an invalid embedding: {value}")
    try:
        return [float(item) for item in value]
    except (TypeError, ValueError) as exc:
        raise EmbeddingError(f"{provider_name} embedding contains non-numeric values") from exc


def _local_hash_embedding(text: str, dimensions: int = 256) -> list[float]:
    vector = [0.0] * dimensions
    tokens = re.findall(r"[A-Za-z_][A-Za-z0-9_.$-]*|[\u4e00-\u9fff]{1,4}", text.lower())
    for token in tokens:
        digest = hashlib.sha256(token.encode("utf-8")).digest()
        index = int.from_bytes(digest[:4], "big") % dimensions
        sign = 1.0 if digest[4] % 2 == 0 else -1.0
        vector[index] += sign
    norm = math.sqrt(sum(value * value for value in vector))
    if norm == 0.0:
        return vector
    return [value / norm for value in vector]
