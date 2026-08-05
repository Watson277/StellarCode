from __future__ import annotations

import threading
from collections import OrderedDict


class SkillContextBuffer:
    def __init__(self, max_skills: int = 3) -> None:
        if max_skills < 1:
            raise ValueError("max_skills must be at least 1")
        self.max_skills = max_skills
        self._entries: OrderedDict[str, str] = OrderedDict()
        self._lock = threading.RLock()

    def push(self, skill_name: str, body: str | None) -> None:
        if not skill_name.strip() or body is None:
            return
        with self._lock:
            self._entries.pop(skill_name, None)
            self._entries[skill_name] = body
            while len(self._entries) > self.max_skills:
                self._entries.popitem(last=False)

    def drain(self) -> str:
        with self._lock:
            if not self._entries:
                return ""
            entries = list(self._entries.items())
            self._entries.clear()
        sections = [
            f"## 已加载 Skill：{name}\n{body.strip()}\n"
            for name, body in entries
        ]
        return "\n".join(sections) + "\n---\n"

    def is_empty(self) -> bool:
        with self._lock:
            return not self._entries

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()

