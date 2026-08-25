"""Append-only MCP audit records with argument redaction at the persistence boundary."""

from __future__ import annotations

import json
import re
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


_SENSITIVE_KEY = re.compile(r"(?:api[_-]?key|authorization|password|secret|token)", re.I)


class McpAuditLog:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._lock = threading.Lock()

    def record(
        self,
        *,
        server: str,
        tool: str,
        namespaced_tool: str,
        arguments: dict[str, Any],
        status: str,
        elapsed_ms: int,
        error: str = "",
    ) -> None:
        entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "server": server,
            "tool": tool,
            "namespaced_tool": namespaced_tool,
            "arguments": _redact(arguments),
            "status": status,
            "elapsed_ms": elapsed_ms,
        }
        if error:
            entry["error"] = error
        line = json.dumps(entry, ensure_ascii=False, separators=(",", ":"))
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as stream:
                stream.write(line + "\n")


def _redact(value: object) -> object:
    if isinstance(value, dict):
        return {
            str(key): "[REDACTED]" if _SENSITIVE_KEY.search(str(key)) else _redact(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact(item) for item in value]
    return value
