from __future__ import annotations

import ipaddress
import socket
import threading
import time
from collections import deque
from collections.abc import Callable, Iterable
from urllib.parse import urlsplit


class NetworkPolicyError(RuntimeError):
    """Raised when an outbound URL violates the fetch security policy."""


Resolver = Callable[[str, int], Iterable[str]]


class NetworkPolicy:
    def __init__(self, resolver: Resolver | None = None) -> None:
        self._resolver = resolver or _resolve_addresses

    def validate_url(self, url: str) -> str:
        normalized = url.strip()
        parsed = urlsplit(normalized)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise NetworkPolicyError(
                "URL must use http:// or https:// and include a host."
            )
        if parsed.username or parsed.password:
            raise NetworkPolicyError("URLs containing credentials are not allowed.")

        host = parsed.hostname.rstrip(".").lower()
        if host == "localhost" or host.endswith(".localhost"):
            raise NetworkPolicyError("Localhost addresses are blocked by the web policy.")

        try:
            port = parsed.port or (443 if parsed.scheme == "https" else 80)
        except ValueError as exc:
            raise NetworkPolicyError(f"URL has an invalid port: {exc}") from exc
        try:
            addresses = list(self._resolver(host, port))
        except (OSError, ValueError) as exc:
            raise NetworkPolicyError(f"Could not resolve host {host}: {exc}") from exc
        if not addresses:
            raise NetworkPolicyError(f"Could not resolve host {host}.")

        for address in addresses:
            try:
                ip = ipaddress.ip_address(address)
            except ValueError as exc:
                raise NetworkPolicyError(
                    f"Host {host} resolved to an invalid address."
                ) from exc
            if not ip.is_global:
                raise NetworkPolicyError(
                    f"Blocked non-public address for {host}: {ip.compressed}"
                )
        return normalized


class SlidingWindowRateLimiter:
    def __init__(
        self,
        max_requests: int = 30,
        window_seconds: float = 60,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if max_requests < 1 or window_seconds <= 0:
            raise ValueError("Rate limit values must be positive.")
        self.max_requests = max_requests
        self.window_seconds = window_seconds
        self._clock = clock
        self._timestamps: deque[float] = deque()
        self._lock = threading.Lock()

    def acquire(self) -> None:
        now = self._clock()
        cutoff = now - self.window_seconds
        with self._lock:
            while self._timestamps and self._timestamps[0] <= cutoff:
                self._timestamps.popleft()
            if len(self._timestamps) >= self.max_requests:
                raise NetworkPolicyError(
                    f"Web fetch rate limit exceeded: {self.max_requests} requests "
                    f"per {self.window_seconds:g} seconds."
                )
            self._timestamps.append(now)


def _resolve_addresses(host: str, port: int) -> list[str]:
    return sorted(
        {
            result[4][0]
            for result in socket.getaddrinfo(
                host,
                port,
                type=socket.SOCK_STREAM,
            )
        }
    )
