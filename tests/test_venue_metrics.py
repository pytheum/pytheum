"""Tests for venue-call counters + the /v1/metrics endpoint.

Covers:
  - SingleFlightCache records request/hit/coalesced/upstream_call per venue
  - coalescing is reflected in the counters (the whole point: measurable)
  - errors are counted and the in-flight slot is cleared
  - the /v1/metrics handler surfaces the snapshot with totals
  - untagged cache calls (venue=None) record nothing (back-compat)
"""
from __future__ import annotations

import asyncio
from typing import Any

import pytest

from pytheum.api.metrics import handle_metrics
from pytheum.trader import metrics as venue_metrics
from pytheum.trader.cache import SingleFlightCache


@pytest.fixture(autouse=True)
def _reset_metrics() -> Any:
    venue_metrics.reset()
    yield
    venue_metrics.reset()


@pytest.mark.asyncio
async def test_upstream_call_counted_on_miss() -> None:
    cache = SingleFlightCache()
    calls = 0

    async def fetch() -> str:
        nonlocal calls
        calls += 1
        return "v"

    await cache.get_or_fetch(("k", 1), 60.0, fetch, venue="kalshi")
    snap = venue_metrics.get_metrics().snapshot()
    k = snap["venues"]["kalshi"]
    assert k["requests"] == 1
    assert k["upstream_calls"] == 1
    assert k["hits"] == 0
    assert k["coalesced"] == 0
    assert calls == 1


@pytest.mark.asyncio
async def test_cache_hit_counted_no_second_upstream() -> None:
    cache = SingleFlightCache()
    calls = 0

    async def fetch() -> str:
        nonlocal calls
        calls += 1
        return "v"

    await cache.get_or_fetch(("k", 1), 60.0, fetch, venue="polymarket")
    await cache.get_or_fetch(("k", 1), 60.0, fetch, venue="polymarket")  # hit
    snap = venue_metrics.get_metrics().snapshot()
    pm = snap["venues"]["polymarket"]
    assert pm["requests"] == 2
    assert pm["upstream_calls"] == 1
    assert pm["hits"] == 1
    assert calls == 1


@pytest.mark.asyncio
async def test_coalesced_concurrent_calls_counted() -> None:
    cache = SingleFlightCache()
    started = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    async def fetch() -> str:
        nonlocal calls
        calls += 1
        started.set()
        await release.wait()
        return "v"

    # First caller fires the upstream and blocks inside fetch.
    task1 = asyncio.create_task(
        cache.get_or_fetch(("k", 1), 60.0, fetch, venue="kalshi")
    )
    await started.wait()
    # Second caller lands while the first is in-flight -> coalesced.
    task2 = asyncio.create_task(
        cache.get_or_fetch(("k", 1), 60.0, fetch, venue="kalshi")
    )
    await asyncio.sleep(0)  # let task2 reach the in-flight join
    release.set()
    r1, r2 = await asyncio.gather(task1, task2)

    assert r1 == r2 == "v"
    assert calls == 1  # only ONE upstream call despite two requests
    snap = venue_metrics.get_metrics().snapshot()
    k = snap["venues"]["kalshi"]
    assert k["requests"] == 2
    assert k["upstream_calls"] == 1
    assert k["coalesced"] == 1


@pytest.mark.asyncio
async def test_error_counted_and_inflight_cleared() -> None:
    cache = SingleFlightCache()

    async def boom() -> str:
        raise RuntimeError("venue down")

    with pytest.raises(RuntimeError):
        await cache.get_or_fetch(("k", 1), 60.0, boom, venue="kalshi")

    snap = venue_metrics.get_metrics().snapshot()
    k = snap["venues"]["kalshi"]
    assert k["upstream_calls"] == 1
    assert k["errors"] == 1

    # A retry must re-fire (in-flight slot cleared on error).
    async def ok() -> str:
        return "v"

    assert await cache.get_or_fetch(("k", 1), 60.0, ok, venue="kalshi") == "v"
    snap2 = venue_metrics.get_metrics().snapshot()
    assert snap2["venues"]["kalshi"]["upstream_calls"] == 2


@pytest.mark.asyncio
async def test_untagged_calls_record_nothing() -> None:
    cache = SingleFlightCache()

    async def fetch() -> str:
        return "v"

    await cache.get_or_fetch(("k", 1), 60.0, fetch)  # no venue= -> untagged
    snap = venue_metrics.get_metrics().snapshot()
    assert snap["totals"]["requests"] == 0
    assert snap["totals"]["upstream_calls"] == 0


def test_snapshot_totals_and_coalesce_ratio() -> None:
    m = venue_metrics.get_metrics()
    m.record_request("kalshi")
    m.record_upstream_call("kalshi")
    m.record_request("kalshi")
    m.record_hit("kalshi")
    m.record_request("polymarket")
    m.record_coalesced("polymarket")
    snap = m.snapshot()
    t = snap["totals"]
    assert t["requests"] == 3
    assert t["upstream_calls"] == 1
    assert t["hits"] == 1
    assert t["coalesced"] == 1
    assert t["coalesce_savings"] == 2  # hits + coalesced
    assert t["coalesce_ratio"] == pytest.approx(2 / 3, abs=1e-4)


def test_unknown_venue_bucketed_as_other() -> None:
    m = venue_metrics.get_metrics()
    m.record_request("manifold")
    m.record_upstream_call("manifold")
    snap = m.snapshot()
    assert snap["venues"]["other"]["requests"] == 1
    assert snap["venues"]["other"]["upstream_calls"] == 1


@pytest.mark.asyncio
async def test_metrics_handler_returns_snapshot() -> None:
    m = venue_metrics.get_metrics()
    m.record_request("kalshi")
    m.record_upstream_call("kalshi")
    status, body = await handle_metrics({})
    assert status == 200
    assert body["venues"]["kalshi"]["upstream_calls"] == 1
    assert "totals" in body
    assert "now" in body
