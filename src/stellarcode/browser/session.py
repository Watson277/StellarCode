"""Mutable browser connection state; policy decisions live in BrowserGuard instead."""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from enum import Enum


class BrowserMode(str, Enum):
    ISOLATED = "isolated"
    SHARED = "shared"


@dataclass
class BrowserSession:
    _mode: BrowserMode = BrowserMode.ISOLATED
    _browser_url: str = ""
    _last_navigated_url: str = ""
    _agent_opened_pages: set[str] = field(default_factory=set)
    _lock: threading.RLock = field(default_factory=threading.RLock, repr=False)

    @property
    def mode(self) -> BrowserMode:
        with self._lock:
            return self._mode

    @property
    def browser_url(self) -> str:
        with self._lock:
            return self._browser_url

    @property
    def last_navigated_url(self) -> str:
        with self._lock:
            return self._last_navigated_url

    @property
    def agent_opened_pages(self) -> tuple[str, ...]:
        with self._lock:
            return tuple(sorted(self._agent_opened_pages))

    def switch_to_isolated(self) -> None:
        with self._lock:
            self._mode = BrowserMode.ISOLATED
            self._browser_url = ""
            self._last_navigated_url = ""
            self._agent_opened_pages.clear()

    def switch_to_shared(self, browser_url: str) -> None:
        with self._lock:
            self._mode = BrowserMode.SHARED
            self._browser_url = browser_url
            self._last_navigated_url = ""
            self._agent_opened_pages.clear()

    def remember_navigation(self, url: str) -> None:
        if not url.strip():
            return
        with self._lock:
            self._last_navigated_url = url.strip()

    def record_opened_page(self, page_id: str | int) -> None:
        normalized = str(page_id).strip()
        if not normalized:
            return
        with self._lock:
            self._agent_opened_pages.add(normalized)

    def is_agent_opened_page(self, page_id: str | int | None) -> bool:
        if page_id is None:
            return False
        with self._lock:
            return str(page_id).strip() in self._agent_opened_pages

    def forget_opened_page(self, page_id: str | int | None) -> None:
        if page_id is None:
            return
        with self._lock:
            self._agent_opened_pages.discard(str(page_id).strip())

    def clear_agent_opened_pages(self) -> None:
        with self._lock:
            self._agent_opened_pages.clear()
