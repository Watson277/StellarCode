"""Typed short-term records and the minimal long-term memory record."""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any


class MemoryType(str, Enum):
    CONVERSATION = "CONVERSATION"
    FACT = "FACT"
    SUMMARY = "SUMMARY"
    TOOL_RESULT = "TOOL_RESULT"


@dataclass(frozen=True)
class ExtractedFact:
    """Transient fact plus its storage route; never persisted as record metadata."""

    content: str
    scope: str


@dataclass
class MemoryEntry:
    """Rich record used only by per-conversation short-term memory."""

    id: str
    content: str
    type: MemoryType
    timestamp: float = field(default_factory=time.time)
    metadata: dict[str, str] = field(default_factory=dict)
    token_count: int = 0

    @classmethod
    def create(
        cls,
        content: str,
        type: MemoryType,
        metadata: dict[str, str] | None = None,
    ) -> "MemoryEntry":
        return cls(
            id=f"mem_{uuid.uuid4().hex[:10]}",
            content=content,
            type=type,
            metadata=metadata or {},
            token_count=estimate_tokens(content),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "content": self.content,
            "type": self.type.value,
            "timestamp": self.timestamp,
            "metadata": self.metadata,
            "token_count": self.token_count,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "MemoryEntry":
        return cls(
            id=str(data["id"]),
            content=str(data["content"]),
            type=MemoryType(str(data["type"])),
            timestamp=float(data.get("timestamp") or time.time()),
            metadata={str(k): str(v) for k, v in dict(data.get("metadata") or {}).items()},
            token_count=int(data.get("token_count") or estimate_tokens(str(data["content"]))),
        )


@dataclass
class LongTermMemoryEntry:
    """One type-free, independently updateable long-term memory record."""

    id: str
    content: str
    embedding: list[float]
    status: str
    created_at: str
    updated_at: str

    @classmethod
    def create(
        cls,
        content: str,
        embedding: list[float] | None = None,
        *,
        status: str = "active",
    ) -> "LongTermMemoryEntry":
        now = utc_timestamp()
        return cls(
            id=f"mem_{uuid.uuid4().hex[:10]}",
            content=content.strip(),
            embedding=[float(value) for value in (embedding or [])],
            status=status,
            created_at=now,
            updated_at=now,
        )

    def to_dict(self) -> dict[str, Any]:
        """Return exactly the V1 long-term memory persistence schema."""

        return {
            "id": self.id,
            "content": self.content,
            "embedding": list(self.embedding),
            "status": self.status,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "LongTermMemoryEntry":
        """Load V1 records and minimally migrate the previous MemoryEntry shape."""

        content = str(data["content"]).strip()
        if not content:
            raise ValueError("long-term memory content must not be empty")
        raw_embedding = data.get("embedding") or []
        if not isinstance(raw_embedding, list):
            raise ValueError("long-term memory embedding must be an array")
        created_at = str(data.get("created_at") or "").strip()
        if not created_at:
            created_at = _legacy_timestamp(data.get("timestamp"))
        updated_at = str(data.get("updated_at") or created_at).strip() or created_at
        status = str(data.get("status") or "active").strip().lower()
        if status not in {"active", "superseded"}:
            raise ValueError(f"unsupported long-term memory status: {status}")
        return cls(
            id=str(data["id"]),
            content=content,
            embedding=[float(value) for value in raw_embedding],
            status=status,
            created_at=created_at,
            updated_at=updated_at,
        )


def estimate_tokens(text: str) -> int:
    if not text:
        return 0
    chinese_chars = sum(1 for char in text if "\u4e00" <= char <= "\u9fff")
    other_chars = len(text) - chinese_chars
    return int((chinese_chars / 1.5) + (other_chars / 4.0) + 0.999)


def utc_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


def _legacy_timestamp(value: object) -> str:
    try:
        timestamp = float(value)
    except (TypeError, ValueError):
        return utc_timestamp()
    return datetime.fromtimestamp(timestamp, timezone.utc).isoformat(
        timespec="milliseconds"
    ).replace("+00:00", "Z")
