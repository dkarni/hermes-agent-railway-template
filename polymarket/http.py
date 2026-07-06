"""Shared async HTTP client: host allowlist, per-host rate limiting, retries.

Safety boundary (DESIGN.md sec 4): only the configured Polymarket hosts plus
may be reached. Any other host raises DisallowedHostError.
"""

from __future__ import annotations

import asyncio
import random
import time
from typing import Awaitable, Callable, Iterable, Mapping

import httpx

RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})
DEFAULT_MAX_RETRIES = 4
DEFAULT_BACKOFF_BASE = 0.5      # seconds
DEFAULT_BACKOFF_CAP = 20.0     # seconds

# Callback signature: (event_type, host, details) -> None. Injectable so callers
# can record data_quality_events without http.py importing the db layer.
QualityCallback = Callable[[str, str, dict], Awaitable[None] | None]


class DisallowedHostError(Exception):
    """Raised when a request targets a host outside the allowlist."""


class TokenBucket:
    """Simple per-host token bucket. rate = tokens/sec, burst = capacity."""

    def __init__(self, rate: float, burst: float | None = None) -> None:
        self._rate = max(rate, 0.001)
        self._capacity = burst if burst is not None else max(rate, 1.0)
        self._tokens = self._capacity
        self._updated = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self, tokens: float = 1.0) -> None:
        async with self._lock:
            while True:
                now = time.monotonic()
                elapsed = now - self._updated
                self._updated = now
                self._tokens = min(self._capacity, self._tokens + elapsed * self._rate)
                if self._tokens >= tokens:
                    self._tokens -= tokens
                    return
                deficit = tokens - self._tokens
                await asyncio.sleep(deficit / self._rate)


class AllowlistClient:
    """Async client enforcing a host allowlist with rate limiting and retries."""

    def __init__(
        self,
        allowed_hosts: Iterable[str],
        *,
        rate_per_second: float = 5.0,
        max_retries: int = DEFAULT_MAX_RETRIES,
        quality_callback: QualityCallback | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout: float = 30.0,
    ) -> None:
        self._allowed = frozenset(allowed_hosts)
        self._rate = rate_per_second
        self._max_retries = max_retries
        self._quality_callback = quality_callback
        self._buckets: dict[str, TokenBucket] = {}
        self._client = httpx.AsyncClient(transport=transport, timeout=timeout)

    async def __aenter__(self) -> "AllowlistClient":
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()

    def _bucket(self, host: str) -> TokenBucket:
        bucket = self._buckets.get(host)
        if bucket is None:
            bucket = TokenBucket(self._rate)
            self._buckets[host] = bucket
        return bucket

    def _check_host(self, url: str) -> str:
        host = httpx.URL(url).host
        if host not in self._allowed:
            raise DisallowedHostError(
                f"host {host!r} is not in the allowlist {sorted(self._allowed)}"
            )
        return host

    async def _emit(self, event_type: str, host: str, details: dict) -> None:
        if self._quality_callback is None:
            return
        result = self._quality_callback(event_type, host, details)
        if asyncio.iscoroutine(result):
            await result

    async def request(
        self,
        method: str,
        url: str,
        *,
        params: Mapping[str, object] | None = None,
        headers: Mapping[str, str] | None = None,
        json: object | None = None,
    ) -> httpx.Response:
        host = self._check_host(url)
        attempt = 0
        while True:
            await self._bucket(host).acquire()
            try:
                response = await self._client.request(
                    method, url, params=params, headers=headers, json=json
                )
            except httpx.TransportError as exc:
                attempt += 1
                if attempt > self._max_retries:
                    await self._emit(
                        "http_transport_error",
                        host,
                        {"url": str(url), "attempts": attempt, "error": str(exc)},
                    )
                    raise
                await asyncio.sleep(self._backoff(attempt))
                continue

            if response.status_code in RETRYABLE_STATUS:
                attempt += 1
                if attempt > self._max_retries:
                    await self._emit(
                        "http_status_error",
                        host,
                        {"url": str(url), "status": response.status_code, "attempts": attempt},
                    )
                    return response
                delay = self._retry_after(response) or self._backoff(attempt)
                if response.status_code == 429:
                    await self._emit(
                        "http_throttled",
                        host,
                        {"url": str(url), "status": 429, "attempt": attempt, "delay": delay},
                    )
                await asyncio.sleep(delay)
                continue

            return response

    async def get_json(
        self,
        url: str,
        *,
        params: Mapping[str, object] | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> object:
        response = await self.request("GET", url, params=params, headers=headers)
        response.raise_for_status()
        return response.json()

    def _backoff(self, attempt: int) -> float:
        base = min(DEFAULT_BACKOFF_CAP, DEFAULT_BACKOFF_BASE * (2 ** (attempt - 1)))
        return base * (0.5 + random.random() * 0.5)  # full-ish jitter

    @staticmethod
    def _retry_after(response: httpx.Response) -> float | None:
        value = response.headers.get("Retry-After")
        if not value:
            return None
        try:
            return float(value)
        except ValueError:
            return None


def make_client(config, *, quality_callback: QualityCallback | None = None,
                transport: httpx.AsyncBaseTransport | None = None) -> AllowlistClient:
    """Build an AllowlistClient from a Config (hosts derived from base URLs)."""
    return AllowlistClient(
        config.allowed_hosts(),
        rate_per_second=float(config.http_rate_limit_per_second),
        quality_callback=quality_callback,
        transport=transport,
    )
