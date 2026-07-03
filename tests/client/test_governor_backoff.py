"""Regression: the concurrency governor must NOT be held during retry backoff.

A request that is backing off (e.g. after a 429) must not occupy a governor slot
while it sleeps — otherwise a single throttled call would stall unrelated in-flight
requests and collapse throughput. This pins the load-bearing property (verified
2026-07-03 during the final review).
"""
from __future__ import annotations

import asyncio
import time

import httpx

from pytheum.client._transport import (
    AsyncTransport,
    RequestSpec,
    RetryConfig,
    build_limits,
    build_timeout,
)


async def test_governor_released_during_backoff() -> None:
    calls = {"a": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/a":
            calls["a"] += 1
            if calls["a"] == 1:  # first A → 429, forcing a 0.3s Retry-After backoff
                return httpx.Response(429, headers={"retry-after": "0.3"}, json={})
            return httpx.Response(200, json={"who": "a"})
        return httpx.Response(200, json={"who": "b"})

    t = AsyncTransport(base_url="https://x", headers={}, limits=build_limits(10, 5),
                       timeout=build_timeout(2, 5, 5, 2), max_concurrency=1, retry=RetryConfig())
    t._client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://x")

    async def timed(path: str) -> float:
        s = time.monotonic()
        await t.request(RequestSpec("GET", path))
        return time.monotonic() - s

    # governor=1: if backoff held the slot, B would wait A's full 0.3s backoff.
    ta, tb = await asyncio.gather(timed("/a"), timed("/b"))
    assert ta >= 0.25, "A should include its ~0.3s backoff"
    assert tb < 0.15, f"B blocked during A's backoff → governor held during sleep (regression): {tb}"
    await t.aclose()
