from __future__ import annotations

import time

import httpx
import pytest

from ..http import AllowlistClient, DisallowedHostError, TokenBucket

HOST = "gamma-api.polymarket.com"
URL = f"https://{HOST}/markets"


@pytest.mark.asyncio
async def test_token_bucket_spacing():
    # 5 tokens/sec, burst 1 -> 4 extra acquisitions cost ~4/5s.
    bucket = TokenBucket(rate=5.0, burst=1.0)
    await bucket.acquire()  # consumes the initial token immediately
    start = time.monotonic()
    for _ in range(4):
        await bucket.acquire()
    elapsed = time.monotonic() - start
    assert elapsed >= 4 / 5 * 0.8  # allow scheduler slack


@pytest.mark.asyncio
async def test_disallowed_host():
    client = AllowlistClient([HOST])
    try:
        with pytest.raises(DisallowedHostError):
            await client.request("GET", "https://evil.example.com/x")
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_retry_honors_retry_after():
    calls = {"n": 0}
    delays: list = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, headers={"Retry-After": "0.2"}, json={})
        return httpx.Response(200, json={"ok": True})

    transport = httpx.MockTransport(handler)
    client = AllowlistClient([HOST], transport=transport, rate_per_second=1000)
    try:
        start = time.monotonic()
        result = await client.get_json(URL)
        elapsed = time.monotonic() - start
        assert result == {"ok": True}
        assert calls["n"] == 2
        assert elapsed >= 0.2 * 0.8  # waited approx the Retry-After
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_no_retry_on_4xx_validation():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(400, json={"error": "bad"})

    transport = httpx.MockTransport(handler)
    client = AllowlistClient([HOST], transport=transport, rate_per_second=1000)
    try:
        response = await client.request("GET", URL)
        assert response.status_code == 400
        assert calls["n"] == 1  # not retried
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_quality_callback_on_repeated_5xx():
    events: list = []

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={})

    async def cb(event_type, host, details):
        events.append((event_type, host, details))

    transport = httpx.MockTransport(handler)
    client = AllowlistClient(
        [HOST], transport=transport, rate_per_second=1000, max_retries=1, quality_callback=cb
    )
    try:
        response = await client.request("GET", URL)
        assert response.status_code == 503
        assert any(e[0] == "http_status_error" for e in events)
    finally:
        await client.aclose()
