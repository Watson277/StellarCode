"""Low-level Chrome DevTools connectivity probes without browser-side effects."""

from __future__ import annotations

from dataclasses import dataclass

import httpx


@dataclass(frozen=True)
class BrowserProbe:
    connected: bool
    browser_url: str = ""
    browser_version: str = ""
    error: str = ""


class BrowserConnectivityCheck:
    def __init__(self, timeout_seconds: float = 2.0) -> None:
        self.timeout_seconds = timeout_seconds

    def probe(self, port: int) -> BrowserProbe:
        if port < 1024 or port > 65535:
            return BrowserProbe(False, error="port must be between 1024 and 65535")
        browser_url = f"http://127.0.0.1:{port}"
        try:
            response = httpx.get(
                f"{browser_url}/json/version",
                timeout=self.timeout_seconds,
                follow_redirects=False,
            )
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            return BrowserProbe(False, error=str(exc))
        version = payload.get("Browser", "") if isinstance(payload, dict) else ""
        return BrowserProbe(True, browser_url=browser_url, browser_version=str(version))
