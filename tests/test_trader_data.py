"""Tests for the live trader-data endpoints and MCP tools.

Coverage:
  - handle_market_book  (kalshi path, PM path, unknown venue, error degradation)
  - handle_market_trades (kalshi path, PM path, unknown venue, error degradation)
  - handle_market_oi    (kalshi path, PM path, unknown venue, error degradation)
  - normalizers: normalize_kalshi_book, normalize_pm_book,
                 normalize_kalshi_trades, normalize_pm_trades,
                 normalize_kalshi_oi, normalize_pm_oi
  - SingleFlightCache: coalescing, TTL cache hit, LRU eviction
  - Stress test: 50 concurrent requests across 5 refs → exactly 5 venue calls
  - Cache-hit path: second wave within TTL → 0 new venue calls
  - MCP registration: t_orderbook, t_recent_trades, t_open_interest
"""
from __future__ import annotations

import asyncio
from typing import Any

import pytest

from pytheum.api.markets_book import handle_market_book
from pytheum.api.markets_oi import handle_market_oi
from pytheum.api.markets_trades import handle_market_trades
from pytheum.trader.cache import SingleFlightCache
from pytheum.trader.normalizers import (
    normalize_kalshi_book,
    normalize_kalshi_oi,
    normalize_kalshi_trades,
    normalize_pm_book,
    normalize_pm_oi,
    normalize_pm_trades,
)
from pytheum.trader.resolve import PmResolved, kalshi_ticker_from_ref

# ─────────────────────────────────────────────────────────────────────────────
# Stub helpers
# ─────────────────────────────────────────────────────────────────────────────

_FAKE_ORDERBOOK_FP = {
    "orderbook_fp": {
        "yes_dollars": [["0.55", "100"], ["0.54", "200"]],
        "no_dollars": [["0.43", "150"], ["0.42", "250"]],
    }
}

_FAKE_PM_BOOK = {
    "bids": [{"price": "0.54", "size": "100"}, {"price": "0.53", "size": "200"}],
    "asks": [{"price": "0.56", "size": "100"}, {"price": "0.57", "size": "200"}],
}

_FAKE_KALSHI_TRADES = {
    "trades": [
        {"yes_price_dollars": "0.55", "count_fp": "10", "taker_side": "yes",
         "created_time": "2026-01-01T00:00:00Z"},
        {"no_price_dollars": "0.45", "count_fp": "5", "taker_side": "no",
         "created_time": "2026-01-01T00:01:00Z"},
    ]
}

_FAKE_PM_TRADES = [
    {"price": "0.55", "size": "10", "side": "BUY", "timestamp": 1700000000},
    {"price": "0.45", "size": "5", "side": "SELL", "timestamp": 1700000060},
]

_FAKE_KALSHI_MARKET = {
    "market": {
        "ticker": "KXTEST-25-YES",
        "open_interest": 12345,
    }
}

_FAKE_PM_OI = [
    {"asset_id": "0xabc", "market": "0xdef", "open_interest_count": "500"},
    {"asset_id": "0xabc2", "market": "0xdef", "open_interest_count": "250"},
]

_RESOLVED = PmResolved(token_id="token123", condition_id="0xcond456")


class _FakeRest:
    """Stub for KalshiRest."""

    def __init__(self, call_counter: dict[str, int] | None = None,
                 latency: float = 0.0) -> None:
        self._calls = call_counter if call_counter is not None else {}
        self._latency = latency

    async def get_orderbook(self, ticker: str, *, depth: int | None = None) -> tuple:
        await asyncio.sleep(self._latency)
        self._calls["get_orderbook"] = self._calls.get("get_orderbook", 0) + 1
        return _FAKE_ORDERBOOK_FP, {}

    async def get_trades_page(self, ticker: str, *, cursor=None, limit=100,
                              min_ts=None, max_ts=None) -> tuple:
        await asyncio.sleep(self._latency)
        self._calls["get_trades_page"] = self._calls.get("get_trades_page", 0) + 1
        return _FAKE_KALSHI_TRADES, {}, None

    async def get_market(self, ticker: str) -> tuple:
        await asyncio.sleep(self._latency)
        self._calls["get_market"] = self._calls.get("get_market", 0) + 1
        return _FAKE_KALSHI_MARKET, {}


class _FakeClob:
    def __init__(self, call_counter: dict[str, int] | None = None,
                 latency: float = 0.0) -> None:
        self._calls = call_counter if call_counter is not None else {}
        self._latency = latency

    async def get_book(self, token_id: str) -> tuple:
        await asyncio.sleep(self._latency)
        self._calls["get_book"] = self._calls.get("get_book", 0) + 1
        return _FAKE_PM_BOOK, {}


class _FakeData:
    def __init__(self, call_counter: dict[str, int] | None = None,
                 latency: float = 0.0) -> None:
        self._calls = call_counter if call_counter is not None else {}
        self._latency = latency

    async def get_trades(self, *, markets=None, event_ids=None, limit=100,
                         offset=None, side=None) -> tuple:
        await asyncio.sleep(self._latency)
        self._calls["get_trades"] = self._calls.get("get_trades", 0) + 1
        return _FAKE_PM_TRADES, {}

    async def get_open_interest(self, markets: list[str]) -> tuple:
        await asyncio.sleep(self._latency)
        self._calls["get_open_interest"] = self._calls.get("get_open_interest", 0) + 1
        return _FAKE_PM_OI, {}


class _FakeGamma:
    """Stub Gamma that returns a fixed resolved market."""

    def __init__(self, call_counter: dict[str, int] | None = None,
                 latency: float = 0.0) -> None:
        self._calls = call_counter if call_counter is not None else {}
        self._latency = latency

    async def get_market_by_slug(self, slug: str) -> tuple:
        await asyncio.sleep(self._latency)
        self._calls["get_market_by_slug"] = self._calls.get("get_market_by_slug", 0) + 1
        return {"clobTokenIds": ["token123"], "conditionId": "0xcond456"}, {}

    async def get_market_by_id(self, *, market_id: str) -> tuple:
        await asyncio.sleep(self._latency)
        self._calls["get_market_by_id"] = self._calls.get("get_market_by_id", 0) + 1
        return {"clobTokenIds": ["token123"], "conditionId": "0xcond456"}, {}

    async def get_market_by_condition_id(self, condition_id: str) -> tuple:
        await asyncio.sleep(self._latency)
        self._calls["get_market_by_condition_id"] = self._calls.get(
            "get_market_by_condition_id", 0) + 1
        return {"clobTokenIds": ["token123"], "conditionId": condition_id}, {}


def _make_kalshi_clients(call_counter: dict[str, int] | None = None,
                         latency: float = 0.0) -> Any:
    class _C:
        kalshi = type("_K", (), {"rest": _FakeRest(call_counter, latency)})()
        polymarket = None
    return _C()


def _make_pm_clients(gamma_calls: dict[str, int] | None = None,
                     data_calls: dict[str, int] | None = None,
                     clob_calls: dict[str, int] | None = None,
                     latency: float = 0.0) -> Any:
    class _C:
        kalshi = None
        polymarket = type("_P", (), {
            "gamma": _FakeGamma(gamma_calls, latency),
            "clob": _FakeClob(clob_calls, latency),
            "data": _FakeData(data_calls, latency),
        })()
    return _C()


# ─────────────────────────────────────────────────────────────────────────────
# Normalizer unit tests
# ─────────────────────────────────────────────────────────────────────────────

def test_normalize_kalshi_book_structure() -> None:
    result = normalize_kalshi_book(_FAKE_ORDERBOOK_FP, ref="kalshi:KXTEST", depth=20)
    assert result["venue"] == "kalshi"
    assert result["source"] == "live"
    assert result["ref"] == "kalshi:KXTEST"
    assert isinstance(result["bids"], list)
    assert isinstance(result["asks"], list)
    assert isinstance(result["top"], dict)
    # bids come from yes_dollars, sorted desc
    assert result["bids"][0][0] >= result["bids"][-1][0]
    # asks sorted asc
    if len(result["asks"]) > 1:
        assert result["asks"][0][0] <= result["asks"][-1][0]


def test_normalize_kalshi_book_top_of_book() -> None:
    result = normalize_kalshi_book(_FAKE_ORDERBOOK_FP, ref="kalshi:KXTEST", depth=20)
    top = result["top"]
    assert top["bid"] is not None
    assert top["ask"] is not None
    assert top["spread"] is not None
    assert top["mid"] is not None
    assert abs(top["spread"] - (top["ask"] - top["bid"])) < 1e-6


def test_normalize_pm_book_structure() -> None:
    result = normalize_pm_book(_FAKE_PM_BOOK, ref="polymarket:slug", depth=20)
    assert result["venue"] == "polymarket"
    assert result["source"] == "live"
    assert len(result["bids"]) == 2
    assert len(result["asks"]) == 2
    # prices are floats (parsed from strings)
    assert isinstance(result["bids"][0][0], float)


def test_normalize_kalshi_trades_parses_dollar_strings() -> None:
    trades = normalize_kalshi_trades(_FAKE_KALSHI_TRADES, limit=100)
    assert len(trades) == 2
    t0 = trades[0]
    assert t0["side"] == "BUY"
    assert isinstance(t0["price"], float)
    assert isinstance(t0["size"], float)
    t1 = trades[1]
    assert t1["side"] == "SELL"


def test_normalize_pm_trades_converts_ms_timestamp() -> None:
    trades = normalize_pm_trades(_FAKE_PM_TRADES, limit=100)
    assert len(trades) == 2
    assert "T" in trades[0]["ts"]  # ISO format
    assert trades[0]["side"] == "BUY"
    assert trades[1]["side"] == "SELL"


def test_normalize_kalshi_oi_extracts_field() -> None:
    result = normalize_kalshi_oi(_FAKE_KALSHI_MARKET, ref="kalshi:KXTEST")
    assert result["open_interest"] == 12345.0
    assert result["venue"] == "kalshi"
    assert result["source"] == "live"


def test_normalize_pm_oi_sums_tokens() -> None:
    result = normalize_pm_oi(_FAKE_PM_OI, ref="polymarket:slug")
    assert result["open_interest"] == 750.0  # 500 + 250
    assert result["venue"] == "polymarket"
    assert result["source"] == "live"


# ─────────────────────────────────────────────────────────────────────────────
# handle_market_book
# ─────────────────────────────────────────────────────────────────────────────

async def test_book_kalshi_path() -> None:
    clients = _make_kalshi_clients()
    cache = SingleFlightCache()
    status, body = await handle_market_book(
        "kalshi:KXTEST-25-YES", {}, clients=clients, _cache=cache
    )
    assert status == 200
    assert body["venue"] == "kalshi"
    assert body["source"] == "live"
    assert "bids" in body
    assert "asks" in body
    assert "top" in body


async def test_book_pm_path() -> None:
    clients = _make_pm_clients(gamma_calls={}, clob_calls={})
    cache = SingleFlightCache()
    status, body = await handle_market_book(
        "polymarket:some-slug", {}, clients=clients, _cache=cache
    )
    assert status == 200
    assert body["venue"] == "polymarket"
    assert body["source"] == "live"


async def test_book_depth_param_parsed() -> None:
    clients = _make_kalshi_clients()
    cache = SingleFlightCache()
    status, body = await handle_market_book(
        "kalshi:KXTEST-25-YES", {"depth": "5"}, clients=clients, _cache=cache
    )
    assert status == 200
    # depth=5 should limit levels
    assert len(body["bids"]) <= 5
    assert len(body["asks"]) <= 5


async def test_book_unknown_venue() -> None:
    cache = SingleFlightCache()
    status, body = await handle_market_book(
        "bareref", {}, clients=object(), _cache=cache
    )
    assert status == 200
    assert body.get("error") == "unknown_venue"


async def test_book_kalshi_error_degrades() -> None:
    class _ErrorRest:
        async def get_orderbook(self, ticker, *, depth=None):
            raise RuntimeError("network timeout")

    class _C:
        kalshi = type("_K", (), {"rest": _ErrorRest()})()
        polymarket = None

    cache = SingleFlightCache()
    status, body = await handle_market_book(
        "kalshi:KXTEST-25-YES", {}, clients=_C(), _cache=cache
    )
    assert status == 200
    assert body.get("error") == "venue_unavailable"
    assert body.get("source") == "unavailable"
    assert "network timeout" in body.get("detail", "")


async def test_book_pm_gamma_error_degrades() -> None:
    class _ErrorGamma:
        async def get_market_by_slug(self, slug):
            raise RuntimeError("gamma down")

    class _C:
        kalshi = None
        polymarket = type("_P", (), {
            "gamma": _ErrorGamma(),
            "clob": _FakeClob(),
            "data": _FakeData(),
        })()

    cache = SingleFlightCache()
    status, body = await handle_market_book(
        "polymarket:some-slug", {}, clients=_C(), _cache=cache
    )
    assert status == 200
    assert body.get("error") == "venue_unavailable"
    assert body.get("source") == "unavailable"


# ─────────────────────────────────────────────────────────────────────────────
# handle_market_trades
# ─────────────────────────────────────────────────────────────────────────────

async def test_trades_kalshi_path() -> None:
    clients = _make_kalshi_clients()
    cache = SingleFlightCache()
    status, body = await handle_market_trades(
        "kalshi:KXTEST-25-YES", {}, clients=clients, _cache=cache
    )
    assert status == 200
    assert body["venue"] == "kalshi"
    assert body["source"] == "live"
    assert isinstance(body["trades"], list)
    assert len(body["trades"]) == 2


async def test_trades_pm_uses_condition_id() -> None:
    data_calls: dict[str, int] = {}
    clients = _make_pm_clients(gamma_calls={}, data_calls=data_calls)
    cache = SingleFlightCache()
    status, body = await handle_market_trades(
        "polymarket:some-slug", {}, clients=clients, _cache=cache
    )
    assert status == 200
    assert body["venue"] == "polymarket"
    assert data_calls.get("get_trades", 0) == 1


async def test_trades_unknown_venue() -> None:
    cache = SingleFlightCache()
    status, body = await handle_market_trades(
        "bareref", {}, clients=object(), _cache=cache
    )
    assert status == 200
    assert body.get("error") == "unknown_venue"


async def test_trades_error_degrades() -> None:
    class _ErrorRest:
        async def get_trades_page(self, ticker, *, cursor=None, limit=100,
                                  min_ts=None, max_ts=None):
            raise ConnectionError("timed out")

    class _C:
        kalshi = type("_K", (), {"rest": _ErrorRest()})()
        polymarket = None

    cache = SingleFlightCache()
    status, body = await handle_market_trades(
        "kalshi:KXTEST-25-YES", {}, clients=_C(), _cache=cache
    )
    assert status == 200
    assert body.get("source") == "unavailable"


# ─────────────────────────────────────────────────────────────────────────────
# handle_market_oi
# ─────────────────────────────────────────────────────────────────────────────

async def test_oi_kalshi_path() -> None:
    clients = _make_kalshi_clients()
    cache = SingleFlightCache()
    status, body = await handle_market_oi(
        "kalshi:KXTEST-25-YES", {}, clients=clients, _cache=cache
    )
    assert status == 200
    assert body["venue"] == "kalshi"
    assert body["source"] == "live"
    assert body["open_interest"] == 12345.0


async def test_oi_pm_path() -> None:
    data_calls: dict[str, int] = {}
    clients = _make_pm_clients(gamma_calls={}, data_calls=data_calls)
    cache = SingleFlightCache()
    status, body = await handle_market_oi(
        "polymarket:some-slug", {}, clients=clients, _cache=cache
    )
    assert status == 200
    assert body["venue"] == "polymarket"
    assert body["open_interest"] == 750.0
    assert data_calls.get("get_open_interest", 0) == 1


async def test_oi_unknown_venue() -> None:
    cache = SingleFlightCache()
    status, body = await handle_market_oi(
        "bareref", {}, clients=object(), _cache=cache
    )
    assert status == 200
    assert body.get("error") == "unknown_venue"


async def test_oi_error_degrades() -> None:
    class _ErrorRest:
        async def get_market(self, ticker):
            raise RuntimeError("upstream error")

    class _C:
        kalshi = type("_K", (), {"rest": _ErrorRest()})()
        polymarket = None

    cache = SingleFlightCache()
    status, body = await handle_market_oi(
        "kalshi:KXTEST-25-YES", {}, clients=_C(), _cache=cache
    )
    assert status == 200
    assert body.get("source") == "unavailable"


# ─────────────────────────────────────────────────────────────────────────────
# SingleFlightCache unit tests
# ─────────────────────────────────────────────────────────────────────────────

async def test_cache_hit_returns_same_result() -> None:
    cache = SingleFlightCache()
    call_count = 0

    async def _fetch() -> str:
        nonlocal call_count
        call_count += 1
        return "result"

    r1 = await cache.get_or_fetch("k", 60.0, _fetch)
    r2 = await cache.get_or_fetch("k", 60.0, _fetch)
    assert r1 == r2 == "result"
    assert call_count == 1  # only one underlying call


async def test_cache_ttl_expired_refetches() -> None:
    cache = SingleFlightCache()
    call_count = 0

    async def _fetch() -> str:
        nonlocal call_count
        call_count += 1
        return f"result_{call_count}"

    r1 = await cache.get_or_fetch("k", 0.0, _fetch)  # TTL=0 → expires immediately
    await asyncio.sleep(0)  # yield so the time advances
    r2 = await cache.get_or_fetch("k", 0.0, _fetch)  # should re-fetch
    assert r1 == "result_1"
    assert r2 == "result_2"
    assert call_count == 2


async def test_cache_lru_eviction() -> None:
    cache = SingleFlightCache(max_size=3)

    async def _val(v: int) -> int:
        return v

    for i in range(4):
        key = f"k{i}"
        captured = i  # avoid late-binding in closures
        await cache.get_or_fetch(key, 60.0, lambda v=captured: _val(v))

    # Size must not exceed max_size after LRU eviction
    assert len(cache._cache) <= 3


async def test_cache_exception_clears_inflight() -> None:
    cache = SingleFlightCache()

    async def _fail() -> None:
        raise ValueError("boom")

    with pytest.raises(ValueError):
        await cache.get_or_fetch("k", 60.0, _fail)

    # in-flight entry must be cleared after exception
    assert "k" not in cache._in_flight


async def test_cache_exception_propagates_to_all_waiters() -> None:
    """When the initiator's fetch raises, all joiners get the same exception."""
    cache = SingleFlightCache()
    results: list[Exception | str] = []

    async def _slow_fail() -> str:
        await asyncio.sleep(0.02)
        raise RuntimeError("kaboom")

    async def _requester() -> None:
        try:
            await cache.get_or_fetch("k", 60.0, _slow_fail)
            results.append("ok")
        except RuntimeError as e:
            results.append(e)

    await asyncio.gather(*[_requester() for _ in range(5)])
    assert len(results) == 5
    assert all(isinstance(r, RuntimeError) for r in results)
    assert "k" not in cache._in_flight


# ─────────────────────────────────────────────────────────────────────────────
# STRESS TEST: 50 concurrent requests, 5 distinct refs → 5 venue calls
# ─────────────────────────────────────────────────────────────────────────────

async def test_coalescing_stress_50_concurrent_5_refs() -> None:
    """50 concurrent requests across 5 refs with 50ms venue latency.

    Coalescing proof: each ref triggers exactly 1 venue call regardless of
    concurrency → total venue calls == 5 (not 50).
    """
    venue_call_counter: dict[str, int] = {}
    cache = SingleFlightCache()

    # Build 5 different tickers; each gets a counter slot
    tickers = [f"KXTEST-{i:02d}-YES" for i in range(5)]
    refs = [f"kalshi:{t}" for t in tickers]

    # Shared rest stub with 50ms latency
    rest = _FakeRest(venue_call_counter, latency=0.05)

    class _C:
        kalshi = type("_K", (), {"rest": rest})()
        polymarket = None

    clients = _C()

    # 10 concurrent requests per ref = 50 total
    tasks = []
    for ref in refs:
        for _ in range(10):
            tasks.append(handle_market_book(ref, {}, clients=clients, _cache=cache))

    results = await asyncio.gather(*tasks)

    # All 50 requests should succeed
    assert all(r[0] == 200 for r in results)
    assert all(r[1].get("source") == "live" for r in results)

    # Coalescing: only 5 venue calls (one per distinct ref), not 50
    total_venue_calls = venue_call_counter.get("get_orderbook", 0)
    assert total_venue_calls == 5, (
        f"Expected 5 venue calls (1 per ref), got {total_venue_calls}. "
        "Coalescing is broken."
    )


async def test_cache_hit_second_wave_zero_calls() -> None:
    """After the first wave warms the cache, a second wave within TTL makes
    zero new venue calls."""
    venue_call_counter: dict[str, int] = {}
    cache = SingleFlightCache()
    refs = ["kalshi:KXBOOK-00", "kalshi:KXBOOK-01"]
    rest = _FakeRest(venue_call_counter, latency=0.0)

    class _C:
        kalshi = type("_K", (), {"rest": rest})()
        polymarket = None

    clients = _C()

    # First wave — warms the cache
    first_wave = [handle_market_book(r, {}, clients=clients, _cache=cache) for r in refs]
    await asyncio.gather(*first_wave)
    calls_after_first = venue_call_counter.get("get_orderbook", 0)
    assert calls_after_first == 2

    # Second wave — all from cache (TTL_BOOK=2s, we haven't waited)
    second_wave = [handle_market_book(r, {}, clients=clients, _cache=cache) for r in refs * 5]
    await asyncio.gather(*second_wave)

    calls_after_second = venue_call_counter.get("get_orderbook", 0)
    assert calls_after_second == 2, (
        f"Expected 2 total calls (second wave from cache), got {calls_after_second}."
    )


# ─────────────────────────────────────────────────────────────────────────────
# resolve.py helpers
# ─────────────────────────────────────────────────────────────────────────────

def test_kalshi_ticker_from_ref() -> None:
    assert kalshi_ticker_from_ref("kalshi:KXTEST-25-YES") == "KXTEST-25-YES"
    assert kalshi_ticker_from_ref("KXTEST-25-YES") == "KXTEST-25-YES"


async def test_resolve_pm_by_slug() -> None:
    from pytheum.trader.resolve import resolve_pm

    calls: dict[str, int] = {}
    gamma = _FakeGamma(calls)
    result = await resolve_pm("some-slug", gamma=gamma)
    assert isinstance(result.token_id, str)
    assert isinstance(result.condition_id, str)
    assert calls.get("get_market_by_slug", 0) == 1


async def test_resolve_pm_by_numeric_id() -> None:
    from pytheum.trader.resolve import resolve_pm

    calls: dict[str, int] = {}
    gamma = _FakeGamma(calls)
    result = await resolve_pm("123456", gamma=gamma)
    assert result.token_id == "token123"
    assert calls.get("get_market_by_id", 0) == 1


async def test_resolve_pm_by_condition_id() -> None:
    from pytheum.trader.resolve import resolve_pm

    calls: dict[str, int] = {}
    gamma = _FakeGamma(calls)
    result = await resolve_pm("0xabc123def456", gamma=gamma)
    assert result.condition_id == "0xabc123def456"
    assert calls.get("get_market_by_condition_id", 0) == 1


# ─────────────────────────────────────────────────────────────────────────────
# Regression tests — real captured venue payload shapes (2026-06-13 diagnosis)
# ─────────────────────────────────────────────────────────────────────────────

def test_normalize_pm_oi_real_venue_shape() -> None:
    """FIX 1 — venue returns {"market": ..., "value": 21870.138673}; key is "value".

    Before fix: normalizer checked open_interest_count / open_interest → None for every item.
    After fix:  falls through to item.get("value") → float populated correctly.

    Payload captured live:
      GET https://data-api.polymarket.com/oi?market=0x1fad72...
    """
    venue_payload = [
        {
            "market": "0x1fad72fae204143ff1c3035e99e7c0f65ea8d5cd9bd1070987bd1a3316f772be",
            "value": 21870.138673,
        }
    ]
    result = normalize_pm_oi(venue_payload, ref="polymarket:new-rhianna-album-before-gta-vi-926")

    assert result["venue"] == "polymarket"
    assert result["source"] == "live"
    # Must NOT be None — this was the broken state before the fix
    assert result["open_interest"] is not None
    assert result["open_interest"] == pytest.approx(21870.138673)


def test_extract_resolved_clob_ids_json_string() -> None:
    """FIX 3 — Gamma returns clobTokenIds as a JSON-encoded STRING, not a Python list.

    Before fix: _extract_resolved treated the string as a list; clob_ids[0] → '[' (first char).
    After fix:  json.loads() decodes the string first; token_id is the correct integer string.

    Type confirmed live: type(m["clobTokenIds"]) → <class 'str'>
    Market: new-rhianna-album-before-gta-vi-926 (Gamma id 540817)
    """
    from pytheum.trader.resolve import _extract_resolved

    # Exact Gamma response shape for market 540817
    gamma_market_dict = {
        "id": "540817",
        "conditionId": "0x1fad72fae204143ff1c3035e99e7c0f65ea8d5cd9bd1070987bd1a3316f772be",
        # Gamma encodes this field as a JSON string, not a native list
        "clobTokenIds": (
            '["98022490269692409998126496127597032490334070080325855126491859374983463996227",'
            '"53831553867117376929679638628984757498953867665706768399789765049888178027684"]'
        ),
    }

    resolved = _extract_resolved(gamma_market_dict)

    # token_id must be the YES token integer string, NOT '[' (the first char of the JSON string)
    assert resolved.token_id == (
        "98022490269692409998126496127597032490334070080325855126491859374983463996227"
    ), f"Got wrong token_id: {resolved.token_id!r} — likely still reading the raw string"
    assert resolved.condition_id == (
        "0x1fad72fae204143ff1c3035e99e7c0f65ea8d5cd9bd1070987bd1a3316f772be"
    )


def test_extract_resolved_clob_ids_malformed_string_raises() -> None:
    """_extract_resolved must raise ValueError (not crash) on unparseable clobTokenIds."""
    from pytheum.trader.resolve import _extract_resolved

    with pytest.raises(ValueError, match="clobTokenIds"):
        _extract_resolved({
            "id": "99999",
            "conditionId": "0xabc",
            "clobTokenIds": "not-valid-json",
        })


def test_extract_resolved_clob_ids_native_list_still_works() -> None:
    """Regression guard: a native Python list (not a string) must still resolve correctly."""
    from pytheum.trader.resolve import _extract_resolved

    resolved = _extract_resolved({
        "id": "123",
        "conditionId": "0xdeadbeef",
        "clobTokenIds": ["tokenA", "tokenB"],
    })
    assert resolved.token_id == "tokenA"
    assert resolved.condition_id == "0xdeadbeef"


# ─────────────────────────────────────────────────────────────────────────────
# MCP tool registration
# ─────────────────────────────────────────────────────────────────────────────

def test_mcp_trader_tools_registered() -> None:
    """t_orderbook, t_recent_trades, t_open_interest are registered on the MCP instance."""
    from pytheum.mcp.server import mcp

    tool_names = {t.name for t in mcp._tool_manager.list_tools()}
    assert "t_orderbook" in tool_names, f"t_orderbook not found in {tool_names}"
    assert "t_recent_trades" in tool_names, f"t_recent_trades not found in {tool_names}"
    assert "t_open_interest" in tool_names, f"t_open_interest not found in {tool_names}"
