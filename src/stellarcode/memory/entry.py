"""Typed memory records and stable identifiers used by all memory stores."""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class MemoryType(str, Enum):
    CONVERSATION = "CONVERSATION"
    FACT = "FACT"
    SUMMARY = "SUMMARY"
    TOOL_RESULT = "TOOL_RESULT"


@dataclass
class MemoryEntry:
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


def estimate_tokens(text: str) -> int:
    if not text:
        return 0
    chinese_chars = sum(1 for char in text if "\u4e00" <= char <= "\u9fff")
    other_chars = len(text) - chinese_chars
    return int((chinese_chars / 1.5) + (other_chars / 4.0) + 0.999)

