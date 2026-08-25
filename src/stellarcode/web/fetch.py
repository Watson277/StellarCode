"""Bounded HTTP fetch pipeline that applies network policy before every request hop."""

from __future__ import annotations

import re
from urllib.parse import quote, unquote, urljoin, urlsplit

import httpx

from stellarcode.web.extractor import HtmlExtractor
from stellarcode.web.network import (
    NetworkPolicy,
    NetworkPolicyError,
    SlidingWindowRateLimiter,
)


class WebFetchError(RuntimeError):
    """Raised when a URL cannot be fetched or safely processed."""


class WebFetcher:
    USER_AGENT = "StellarCode-Python/0.1 (+https://github.com/stellarcode)"

    def __init__(
        self,
        network_policy: NetworkPolicy | None = None,
        rate_limiter: SlidingWindowRateLimiter | None = None,
        extractor: HtmlExtractor | None = None,
        transport: httpx.BaseTransport | None = None,
        max_body_bytes: int = 5 * 1024 * 1024,
        max_redirects: int = 5,
    ) -> None:
        self.network_policy = network_policy or NetworkPolicy()
        self.rate_limiter = rate_limiter or SlidingWindowRateLimiter()
        self.extractor = extractor or HtmlExtractor()
        self.transport = transport
        self.max_body_bytes = max_body_bytes
        self.max_redirects = max_redirects

    def fetch(
        self,
        url: str,
        max_chars: int = 8000,
        timeout_seconds: int = 30,
    ) -> str:
        character_limit = max(1, min(int(max_chars), 100000))
        timeout = max(1, min(int(timeout_seconds), 120))
        current_url = _rewrite_wikipedia_url(url.strip())

        try:
            self.rate_limiter.acquire()
            with httpx.Client(
                timeout=timeout,
                follow_redirects=False,
                transport=self.transport,
                headers={"User-Agent": self.USER_AGENT},
            ) as client:
                for redirect_count in range(self.max_redirects + 1):
                    current_url = _rewrite_wikipedia_url(current_url)
                    current_url = self.network_policy.validate_url(current_url)
                    with client.stream("GET", current_url) as response:
                        if response.is_redirect:
                            if redirect_count >= self.max_redirects:
                                raise WebFetchError(
                                    f"Too many redirects (maximum {self.max_redirects})."
                                )
                            location = response.headers.get("location")
                            if not location:
                                raise WebFetchError(
                                    "Redirect response did not include a Location header."
                                )
                            current_url = urljoin(str(response.url), location)
                            continue

                        response.raise_for_status()
                        body, byte_truncated = self._read_body(response)
                        final_url = str(response.url)
                        status_code = response.status_code
                        content_type = response.headers.get("content-type", "unknown")
                        encoding = _encoding_from_content_type(content_type)
                    break
                else:
                    raise WebFetchError("Web request did not produce a response.")
        except NetworkPolicyError as exc:
            raise WebFetchError(f"Web request blocked: {exc}") from exc
        except httpx.HTTPError as exc:
            raise WebFetchError(f"Web request failed: {exc}") from exc

        text = body.decode(encoding or "utf-8", errors="replace")
        if "html" in content_type.lower():
            content = self.extractor.extract(text)
            if not content:
                content = (
                    "[No readable static page content was found. The page may require "
                    "JavaScript rendering or may be blocking automated requests.]"
                )
        else:
            content = text.strip()

        char_truncated = len(content) > character_limit
        content = content[:character_limit]
        truncation_notes = []
        if byte_truncated:
            truncation_notes.append(
                f"response body exceeded {self.max_body_bytes} bytes"
            )
        if char_truncated:
            truncation_notes.append(f"content truncated at {character_limit} chars")
        if truncation_notes:
            content = f"{content}\n...[{'; '.join(truncation_notes)}]"

        return (
            f"URL: {final_url}\n"
            f"Status: {status_code}\n"
            f"Content-Type: {content_type}\n\n"
            f"{content}"
        )

    def _read_body(self, response: httpx.Response) -> tuple[bytes, bool]:
        chunks: list[bytes] = []
        size = 0
        truncated = False
        for chunk in response.iter_bytes(chunk_size=8192):
            remaining = self.max_body_bytes - size
            if remaining <= 0:
                truncated = True
                break
            if len(chunk) > remaining:
                chunks.append(chunk[:remaining])
                truncated = True
                break
            chunks.append(chunk)
            size += len(chunk)
        return b"".join(chunks), truncated


def _encoding_from_content_type(content_type: str) -> str | None:
    match = re.search(r"charset=([^\s;]+)", content_type, re.IGNORECASE)
    return match.group(1).strip("\"'") if match else None


def _rewrite_wikipedia_url(url: str) -> str:
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    match = re.fullmatch(r"([a-z-]{2,12})\.wikipedia\.org", host)
    if match is None or not parts.path.startswith("/wiki/"):
        return url
    language = match.group(1)
    page_key = unquote(parts.path.removeprefix("/wiki/")).strip()
    if not page_key:
        return url
    return (
        "https://api.wikimedia.org/core/v1/wikipedia/"
        f"{language}/page/{quote(page_key, safe='')}/html"
    )
