"""Tests for the ``status=live|settled|all`` filter on /v1/markets/equivalents.

The collection endpoint was hardcoded live-only (skip row-stale, drop
book-stale legs, drop one-sided pairs).  This adds a ``status`` param:

- ``live``    (default) — exact legacy behaviour, all other tests stay green.
- ``settled`` — only settled pairs (EITHER hydrated leg is_stale); no row-stale
                skip up front, no one-sided drop (settled legs are commonly
                one-sided / unquoted); live pairs are dropped instead.
- ``all``     — no resolution filter, no one-sided drop; every hydrated pair.

Reuses the in-memory _HydratingDao / _make_index harness from
test_equivalents_overfetch.py (no disk / DB).
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from pytheum.api.markets_equivalents import _cache, handle_markets_equivalents


def _iso(days_from_now: float) -> str:
    return (datetime.now(UTC) + timedelta(days=days_from_now)).isoformat()


def _row(n: int, *, days: float) -> dict:
    return {
        "kalshi_ref": f"kalshi:K{n}",
        "kalshi_ticker": f"K{n}",
        "pm_ref": f"polymarket:{n}",
        "pm_gamma_id": str(n),
        "method": "structured_key",
        "confidence": 1.0,
        "bet_type": "moneyline",
        "resolution_date": _iso(days),
    }


_BOOK_PAYLOAD = '{"bestBid": 0.45, "bestAsk": 0.55}'


class _HydratingDao:
    def __init__(self, res_at: dict[str, str], no_book: set[str] | None = None) -> None:
        self._res_at = res_at
        self._no_book = no_book or set()

    async def fetch_markets_by_ids(self, ids: list[str]) -> list[dict]:
        out = []
        for i in ids:
            out.append({
                "id": i,
                "question": f"Q {i}",
                "venue": i.split(":")[0],
                "status": "active",
                "volume_usd": 1_000_000.0,
                "resolution_at": self._res_at.get(i),
                "payload": None if i in self._no_book else _BOOK_PAYLOAD,
            })
        return out

    async def fetch_equivalence_pairs(self, limit: int = 50) -> list[dict]:
        return []


def _make_index(rows: list[dict]):
    from pytheum.equivalence.index import EquivalenceIndex

    idx = EquivalenceIndex()
    idx.dataset_version = "2026-06-22T00:00:00Z"
    idx._rows.extend(rows)
    return idx


def _res_at_for(rows: list[dict]) -> dict[str, str]:
    res_at = {r["pm_ref"]: r["resolution_date"] for r in rows}
    res_at.update({r["kalshi_ref"]: r["resolution_date"] for r in rows})
    return res_at


# --------------------------------------------------------------------------- #
# Default == live (unchanged behaviour)
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_default_status_is_live_and_drops_stale():
    _cache.clear()
    resolved = [_row(i, days=-3) for i in range(10)]
    live = [_row(1000 + i, days=+14) for i in range(10)]
    rows = resolved + live
    idx = _make_index(rows)
    dao = _HydratingDao(_res_at_for(rows))

    status, body = await handle_markets_equivalents(
        {"limit": "5"}, dao=dao, equivalence=idx, force_refresh=True,
    )
    assert status == 200
    assert body["meta"]["status"] == "live"
    assert body["count"] == 5
    for p in body["pairs"]:
        assert p["a"]["is_stale"] is False
        assert p["b"]["is_stale"] is False


@pytest.mark.asyncio
async def test_explicit_status_live_matches_default():
    _cache.clear()
    live = [_row(1000 + i, days=+14) for i in range(10)]
    idx = _make_index(live)
    dao = _HydratingDao(_res_at_for(live))

    _, body = await handle_markets_equivalents(
        {"limit": "5", "status": "LIVE"}, dao=dao, equivalence=idx, force_refresh=True,
    )
    assert body["meta"]["status"] == "live"
    assert body["count"] == 5


# --------------------------------------------------------------------------- #
# status=settled
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_status_settled_returns_only_stale_pairs():
    _cache.clear()
    resolved = [_row(i, days=-3) for i in range(8)]
    live = [_row(1000 + i, days=+14) for i in range(8)]
    rows = resolved + live
    idx = _make_index(rows)
    dao = _HydratingDao(_res_at_for(rows))

    _, body = await handle_markets_equivalents(
        {"limit": "20", "status": "settled"}, dao=dao, equivalence=idx, force_refresh=True,
    )
    assert body["meta"]["status"] == "settled"
    assert body["count"] == 8  # only the resolved ones
    for p in body["pairs"]:
        assert p["a"]["is_stale"] or p["b"]["is_stale"]
    # live pairs were dropped, counted in a dedicated counter.
    assert body["meta"]["dropped_live"] >= 8
    assert body["meta"]["dropped_one_sided"] == 0


@pytest.mark.asyncio
async def test_status_settled_includes_one_sided_pairs():
    _cache.clear()
    # settled pairs whose PM leg has NO book (settled legs are commonly unquoted).
    settled = [_row(i, days=-3) for i in range(6)]
    idx = _make_index(settled)
    no_book = {r["pm_ref"] for r in settled}
    dao = _HydratingDao(_res_at_for(settled), no_book=no_book)

    _, body = await handle_markets_equivalents(
        {"limit": "20", "status": "settled"}, dao=dao, equivalence=idx, force_refresh=True,
    )
    assert body["meta"]["status"] == "settled"
    assert body["count"] == 6  # one-sided settled pairs are NOT dropped
    assert body["meta"]["dropped_one_sided"] == 0


# --------------------------------------------------------------------------- #
# status=all
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_status_all_returns_live_and_settled_no_one_sided_drop():
    _cache.clear()
    resolved = [_row(i, days=-3) for i in range(5)]
    live = [_row(1000 + i, days=+14) for i in range(5)]
    rows = resolved + live
    idx = _make_index(rows)
    # make some PM legs bookless to prove one-sided pairs are NOT dropped.
    no_book = {r["pm_ref"] for r in resolved}
    dao = _HydratingDao(_res_at_for(rows), no_book=no_book)

    _, body = await handle_markets_equivalents(
        {"limit": "50", "status": "all"}, dao=dao, equivalence=idx, force_refresh=True,
    )
    assert body["meta"]["status"] == "all"
    assert body["count"] == 10  # every hydrated pair, live + settled
    assert body["meta"]["dropped_stale"] == 0
    assert body["meta"]["dropped_one_sided"] == 0
    stales = [p for p in body["pairs"] if p["a"]["is_stale"] or p["b"]["is_stale"]]
    lives = [p for p in body["pairs"]
             if not (p["a"]["is_stale"] or p["b"]["is_stale"])]
    assert len(stales) == 5
    assert len(lives) == 5


# --------------------------------------------------------------------------- #
# validation + cache-key isolation
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_invalid_status_returns_400():
    _cache.clear()
    idx = _make_index([_row(1, days=+14)])
    dao = _HydratingDao(_res_at_for([_row(1, days=+14)]))

    status, body = await handle_markets_equivalents(
        {"status": "foo"}, dao=dao, equivalence=idx, force_refresh=True,
    )
    assert status == 400
    assert "error" in body
    for tok in ("live", "settled", "all"):
        assert tok in body["error"]


@pytest.mark.asyncio
async def test_status_cache_keys_do_not_collide():
    _cache.clear()
    resolved = [_row(i, days=-3) for i in range(5)]
    live = [_row(1000 + i, days=+14) for i in range(5)]
    rows = resolved + live
    idx = _make_index(rows)
    dao = _HydratingDao(_res_at_for(rows))

    # populate cache for all three statuses (no force_refresh after first write).
    _, live_body = await handle_markets_equivalents(
        {"limit": "50", "status": "live"}, dao=dao, equivalence=idx, force_refresh=True,
    )
    _, settled_body = await handle_markets_equivalents(
        {"limit": "50", "status": "settled"}, dao=dao, equivalence=idx, force_refresh=True,
    )
    _, all_body = await handle_markets_equivalents(
        {"limit": "50", "status": "all"}, dao=dao, equivalence=idx, force_refresh=True,
    )
    # re-read from cache (no force_refresh) — must return each status's own body.
    _, live_cached = await handle_markets_equivalents(
        {"limit": "50", "status": "live"}, dao=dao, equivalence=idx,
    )
    _, settled_cached = await handle_markets_equivalents(
        {"limit": "50", "status": "settled"}, dao=dao, equivalence=idx,
    )
    _, all_cached = await handle_markets_equivalents(
        {"limit": "50", "status": "all"}, dao=dao, equivalence=idx,
    )
    assert live_cached["meta"]["status"] == "live"
    assert settled_cached["meta"]["status"] == "settled"
    assert all_cached["meta"]["status"] == "all"
    assert live_cached["count"] == 5
    assert settled_cached["count"] == 5
    assert all_cached["count"] == 10
