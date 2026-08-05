from __future__ import annotations

import re
from typing import Any

from stellarcode.browser.session import BrowserMode, BrowserSession
from stellarcode.tools.registry import ToolExecutionError


CHROME_TOOL_PREFIX = "mcp__chrome-devtools__"
_SELECTED_PAGE_PATTERN = re.compile(r"(?m)^\s*(\d+):.*\[selected\]\s*$")


class BrowserGuard:
    """Tracks browser state and protects user-owned tabs in shared mode."""

    def __init__(self, session: BrowserSession) -> None:
        self.session = session

    def before_call(self, tool_name: str, arguments: dict[str, Any]) -> None:
        if not tool_name.startswith(CHROME_TOOL_PREFIX):
            return
        local_name = tool_name.removeprefix(CHROME_TOOL_PREFIX)
        if local_name != "close_page" or self.session.mode != BrowserMode.SHARED:
            return
        page_id = _page_id(arguments)
        if not self.session.is_agent_opened_page(page_id):
            raise ToolExecutionError(
                "Shared browser protection blocked close_page: StellarCode may only close "
                "tabs that it opened during this shared session. Close user-owned tabs "
                "manually in Chrome."
            )

    def after_call(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        result: str,
    ) -> None:
        if not tool_name.startswith(CHROME_TOOL_PREFIX):
            return
        local_name = tool_name.removeprefix(CHROME_TOOL_PREFIX)
        if local_name in {"navigate_page", "new_page"}:
            url = arguments.get("url")
            if isinstance(url, str):
                self.session.remember_navigation(url)
        if local_name == "new_page":
            page_id = _page_id(arguments) or _selected_page_id(result)
            if page_id is not None:
                self.session.record_opened_page(page_id)
        elif local_name == "close_page":
            self.session.forget_opened_page(_page_id(arguments))


def _page_id(arguments: dict[str, Any]) -> str | None:
    for key in ("pageId", "pageIdx", "uid"):
        value = arguments.get(key)
        if value is not None and str(value).strip():
            return str(value).strip()
    return None


def _selected_page_id(result: str) -> str | None:
    matches = _SELECTED_PAGE_PATTERN.findall(result or "")
    return matches[-1] if matches else None
