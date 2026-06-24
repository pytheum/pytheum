"""Short-TTL param-keyed response cache for GET /v1/markets/screen.

A load test showed /v1/markets/screen is the serving concurrency ceiling: p50
152ms -> 1056ms as concurrency goes 1->25 because every request runs a live
Supabase query (no cache), while the already-cached /v1/markets/equivalents
stays flat. This mirrors the equivalents cache: a 20s param-keyed response
cache so repeated/popular param-combos don't re-query the dao.

Handler is called directly with a fake DAO that COUNTS screen_markets calls.
"""
from __future__ import annotations

from typing import Any

import pytheum.api.markets_screen as screen_mod
from pytheum.api.markets_screen import handle_markets_screen


class _CountingDao:
    """DAO that counts screen_markets invocations (cache hit -> count stays put)."""

    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows
        self.calls = 0
        self.last_kwargs: dict[str, Any] = {}

    async def screen_markets(self, **kwargs: Any) -> list[dict[str, Any]]:
        self.calls += 1
        self.last_kwargs = kwargs
        return list(self._rows)


def _market(mid: str, venue: str = "polymarket", **over: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "id": mid,
        "question": over.pop("question", f"q-{mid}"),
        "venue": venue,
        "status": over.pop("status", "active"),
        "volume_usd": over.pop("volume_usd", 1000.0),
        "liquidity_usd": 500.0,
        "url": None,
        "resolution_at": over.pop("resolution_at", None),
        "payload": over.pop("payload", {"outcomePrices": "[0.6, 0.4]"}),
    }
    row.update(over)
    return row


def _clear_cache() -> None:
    screen_mod._screen_cache.clear()


# --------------------------------------------------------------------------- #
# (a) second identical call within TTL does NOT re-query the dao
# --------------------------------------------------------------------------- #


async def test_identical_call_hits_cache_no_requery() -> None:
    _clear_cache()
    dao = _CountingDao([_market("polymarket:1"), _market("polymarket:2")])
    status1, body1 = await handle_markets_screen({"limit": "10"}, dao=dao)
    assert status1 == 200
    assert dao.calls == 1

    status2, body2 = await handle_markets_screen({"limit": "10"}, dao=dao)
    assert status2 == 200
    # No re-query: the dao was hit exactly once across both calls.
    assert dao.calls == 1
    # Cached body is identical to the live body.
    assert body2 == body1


# --------------------------------------------------------------------------- #
# (b) different params -> separate cache entry (dao re-queried)
# --------------------------------------------------------------------------- #


async def test_different_sort_by_misses_cache() -> None:
    _clear_cache()
    dao = _CountingDao([_market("polymarket:1")])
    await handle_markets_screen({"sort_by": "volume"}, dao=dao)
    assert dao.calls == 1
    await handle_markets_screen({"sort_by": "liquidity"}, dao=dao)
    assert dao.calls == 2  # different sort_by -> miss


async def test_different_limit_misses_cache() -> None:
    _clear_cache()
    dao = _CountingDao([_market("polymarket:1")])
    await handle_markets_screen({"limit": "10"}, dao=dao)
    assert dao.calls == 1
    await handle_markets_screen({"limit": "20"}, dao=dao)
    assert dao.calls == 2


async def test_different_venue_misses_cache() -> None:
    _clear_cache()
    dao = _CountingDao([_market("kalshi:1", "kalshi")])
    await handle_markets_screen({"venue": "kalshi"}, dao=dao)
    assert dao.calls == 1
    await handle_markets_screen({"venue": "polymarket"}, dao=dao)
    assert dao.calls == 2


async def test_different_numeric_filters_miss_cache() -> None:
    _clear_cache()
    dao = _CountingDao([_market("polymarket:1")])
    await handle_markets_screen({"min_volume": "1000"}, dao=dao)
    assert dao.calls == 1
    await handle_markets_screen({"min_volume": "2000"}, dao=dao)
    assert dao.calls == 2
    await handle_markets_screen({"max_volume": "5000"}, dao=dao)
    assert dao.calls == 3
    await handle_markets_screen({"min_liquidity": "100"}, dao=dao)
    assert dao.calls == 4
    await handle_markets_screen({"resolves_before": "2026-07-01"}, dao=dao)
    assert dao.calls == 5
    await handle_markets_screen({"resolves_after": "2026-06-01"}, dao=dao)
    assert dao.calls == 6
    await handle_markets_screen({"exclude_stale": "true"}, dao=dao)
    assert dao.calls == 7
    await handle_markets_screen({"status": "any"}, dao=dao)
    assert dao.calls == 8


# --------------------------------------------------------------------------- #
# (c) force_refresh bypasses the cache
# --------------------------------------------------------------------------- #


async def test_force_refresh_bypasses_cache() -> None:
    _clear_cache()
    dao = _CountingDao([_market("polymarket:1")])
    await handle_markets_screen({"limit": "10"}, dao=dao)
    assert dao.calls == 1
    # identical params, but force_refresh re-queries
    await handle_markets_screen({"limit": "10"}, dao=dao, force_refresh=True)
    assert dao.calls == 2


# --------------------------------------------------------------------------- #
# (d) venue vs venues alias keys consistently (same effective filter -> hit)
# --------------------------------------------------------------------------- #


async def test_venue_and_venues_alias_share_cache_entry() -> None:
    _clear_cache()
    dao = _CountingDao([_market("kalshi:1", "kalshi")])
    _, body_a = await handle_markets_screen({"venues": "kalshi"}, dao=dao)
    assert dao.calls == 1
    # `venue` alias resolves to the same effective filter -> cache hit, no requery.
    _, body_b = await handle_markets_screen({"venue": "kalshi"}, dao=dao)
    assert dao.calls == 1
    assert body_b == body_a


# --------------------------------------------------------------------------- #
# Degraded (dao=None) path is NOT cached
# --------------------------------------------------------------------------- #


async def test_degraded_no_dao_not_cached() -> None:
    _clear_cache()
    status, body = await handle_markets_screen({}, dao=None)
    assert status == 200
    assert body["meta"]["degraded"] is True
    # Nothing cached for the degraded path: a subsequent real dao call queries.
    dao = _CountingDao([_market("polymarket:1")])
    await handle_markets_screen({}, dao=dao)
    assert dao.calls == 1
