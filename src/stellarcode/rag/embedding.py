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
        model: str | None = None,
        base_url: str | None = None,
        api_key: str | None = None,
        timeout_seconds: float = 120.0,
    ) -> None:
        self.model = (
            model
            or os.getenv("EMBEDDING_MODEL_NAME")
            or os.getenv("EMBEDDING_MODEL")
            or "local-hash-256"
        )
        self.base_url = (base_url or os.getenv("EMBEDDING_BASE_URL") or "").rstrip("/")
        self.api_key = api_key if api_key is not None else os.getenv("EMBEDDING_API_KEY", "")
        self.timeout_seconds = timeout_seconds
        self.provider = "openai-compatible" if self.base_url else "local"

    def embed(self, text: str | None) -> list[float]:
        if not text:
            return []
        content = text[: self.MAX_INPUT_CHARS]
        if not self.base_url:
            return _local_hash_embedding(content)
        return self._embed_openai_compatible(content)

    def _embed_openai_compatible(self, text: str) -> list[float]:
        data = self._post_json(
            _embeddings_url(self.base_url),
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


def _embeddings_url(base_url: str) -> str:
    normalized = base_url.strip().rstrip("/")
    if normalized.endswith("/embeddings"):
        return normalized
    return normalized + "/embeddings"


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
