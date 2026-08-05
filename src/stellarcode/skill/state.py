from __future__ import annotations

import json
import os
import threading
from pathlib import Path


class SkillStateStore:
    """Persists only disabled names so newly installed skills start enabled."""

    def __init__(self, file: str | Path) -> None:
        self.file = Path(file)
        self._lock = threading.RLock()
        self._warnings: list[str] = []

    def disabled(self) -> frozenset[str]:
        with self._lock:
            if not self.file.exists():
                return frozenset()
            try:
                data = json.loads(self.file.read_text(encoding="utf-8"))
                values = data.get("disabled", []) if isinstance(data, dict) else []
                return frozenset(
                    value.strip()
                    for value in values
                    if isinstance(value, str) and value.strip()
                )
            except (OSError, json.JSONDecodeError) as exc:
                self._warn(
                    f"could not parse {self.file}; disabled list ignored: {exc}"
                )
                return frozenset()

    def disable(self, name: str) -> bool:
        with self._lock:
            values = set(self.disabled())
            values.add(name)
            return self._write(values)

    def enable(self, name: str) -> bool:
        with self._lock:
            values = set(self.disabled())
            values.discard(name)
            return self._write(values)

    def warnings(self) -> tuple[str, ...]:
        with self._lock:
            return tuple(self._warnings)

    def _write(self, disabled: set[str]) -> bool:
        temporary = self.file.with_suffix(f"{self.file.suffix}.tmp")
        content = json.dumps(
            {"disabled": sorted(disabled)}, ensure_ascii=False, indent=2
        )
        try:
            self.file.parent.mkdir(parents=True, exist_ok=True)
            temporary.write_text(f"{content}\n", encoding="utf-8")
            os.replace(temporary, self.file)
            return True
        except OSError as exc:
            self._warn(f"could not write {self.file}: {exc}")
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
            return False

    def _warn(self, message: str) -> None:
        if message not in self._warnings:
            self._warnings.append(message)
