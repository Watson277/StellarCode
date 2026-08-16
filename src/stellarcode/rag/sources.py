from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class RagSourceStore:
    """Project-local desktop metadata for RAG sources and the last rebuild."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def snapshot(self) -> dict[str, Any]:
        payload = self._read()
        return {
            "sources": list(payload.get("sources") or []),
            "last_indexed_at": payload.get("last_indexed_at"),
            "last_result": payload.get("last_result"),
            "embedding_provider": str(payload.get("embedding_provider") or ""),
            "embedding_model": str(payload.get("embedding_model") or ""),
        }

    def add(self, paths: list[str | Path]) -> list[dict[str, str]]:
        payload = self._read()
        previous_sources = list(payload.get("sources") or [])
        sources = {
            os.path.normcase(str(Path(item["path"]).resolve())): item
            for item in payload.get("sources") or []
            if isinstance(item, dict) and item.get("path")
        }
        now = _timestamp()
        for value in paths:
            path = Path(value).resolve()
            key = os.path.normcase(str(path))
            sources[key] = {
                "path": str(path),
                "kind": "file" if path.is_file() else "directory",
                "added_at": str(sources.get(key, {}).get("added_at") or now),
            }
        payload["sources"] = sorted(sources.values(), key=lambda item: item["path"].lower())
        if payload["sources"] != previous_sources:
            self._invalidate_index_metadata(payload)
        self._write(payload)
        return list(payload["sources"])

    def remove(self, path: str | Path) -> list[dict[str, str]]:
        payload = self._read()
        previous_sources = list(payload.get("sources") or [])
        target = os.path.normcase(str(Path(path).resolve()))
        payload["sources"] = [
            item
            for item in payload.get("sources") or []
            if os.path.normcase(str(Path(item["path"]).resolve())) != target
        ]
        if payload["sources"] != previous_sources:
            self._invalidate_index_metadata(payload)
        self._write(payload)
        return list(payload["sources"])

    def record_index(
        self,
        result: dict[str, Any],
        *,
        embedding_provider: str,
        embedding_model: str,
    ) -> None:
        payload = self._read()
        payload.update(
            {
                "last_indexed_at": _timestamp(),
                "last_result": result,
                "embedding_provider": embedding_provider,
                "embedding_model": embedding_model,
            }
        )
        self._write(payload)

    def clear_index_metadata(self) -> None:
        payload = self._read()
        self._invalidate_index_metadata(payload)
        self._write(payload)

    @staticmethod
    def _invalidate_index_metadata(payload: dict[str, Any]) -> None:
        for key in ("last_indexed_at", "last_result", "embedding_provider", "embedding_model"):
            payload.pop(key, None)

    def _read(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"version": 1, "sources": []}
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {"version": 1, "sources": []}
        if not isinstance(payload, dict):
            return {"version": 1, "sources": []}
        payload.setdefault("version", 1)
        raw_sources = payload.get("sources")
        payload["sources"] = [
            {
                "path": str(item["path"]),
                "kind": "file" if item.get("kind") == "file" else "directory",
                "added_at": str(item.get("added_at") or ""),
            }
            for item in (raw_sources if isinstance(raw_sources, list) else [])
            if isinstance(item, dict)
            and isinstance(item.get("path"), str)
            and item["path"].strip()
        ]
        return payload

    def _write(self, payload: dict[str, Any]) -> None:
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        temporary.replace(self.path)


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
