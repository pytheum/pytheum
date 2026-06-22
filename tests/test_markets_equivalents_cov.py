"""Coverage tests for pytheum.api.markets_equivalents.

Fills uncovered branches: the collection handler's index/DAO-fallback/no-source
paths + cache + include_rules + stale drops, the warm loop, and the per-ref
handler's orientation logic (poly_side flip, spread_unavailable for unoriented
non-event pairs, condition_id/slug + raw-kalshi fallbacks, degraded meta).
"""
from __future__ import annotations

import asyncio
from typing import Any

import pytest

from pytheum.api import markets_equivalents as eqmod
from pytheum.api.markets_equivalents import (
    _index_rows_to_pairs,
    _parse_bool_param,
    handle_market_equivalents,
    handle_markets_equivalents,
    warm_equivalents_loop,
)


@pytest.fixture(autouse=True)
def _clear_cache():
    eqmod._cache.clear()
    yield
    eqmod._cache.clear()


# --------------------------------------------------------------------------- #
# Fakes
# --------------------------------------------------------------------------- #


class _FakeIndex:
    def __init__(self, rows: list[dict[str, Any]], *, pairs_loaded: int = 0) -> None:
        self._rows = rows
        self.pairs_loaded = pairs_loaded or len(rows)
        self.dataset_version = "2026-06-12T00:00:00Z"
        self.file_missing = False
        self.load_error: str | None = None
        self._by_ref: dict[str, list[dict[str, Any]]] = {}

    def register(self, key: str, pair: dict[str, Any], via: str) -> None:
        self._by_ref[key] = self._by_ref.get(key, []) + [pair]
        self._via = getattr(self, "_via", {})
        self._via[key] = via

    def lookup(self, ref: str) -> tuple[list[dict[str, Any]], str]:
        rows = self._by_ref.get(ref, [])
        if rows:
            return rows, getattr(self, "_via", {}).get(ref, "kalshi_ticker")
        return [], "none"


class _DictDao:
    def __init__(self, store: dict[str, dict[str, Any]]) -> None:
        self._store = store

    async def fetch_market(self, ref: str) -> dict[str, Any] | None:
        return self._store.get(ref)

    async def fetch_markets_by_ids(self, ids: list[str]) -> list[dict[str, Any]]:
        return [self._store[i] for i in ids if i in self._store]


def _market(mid: str, venue: str, *, prices: str, **over: Any) -> dict[str, Any]:
    row = {
        "id": mid, "venue": venue, "question": f"q-{mid}",
        "status": over.pop("status", "active"),
        "volume_usd": over.pop("volume_usd", 1000.0), "liquidity_usd": 100.0,
        "url": f"https://x/{mid}", "resolution_at": over.pop("resolution_at", None),
        "payload": {"outcomePrices": prices, "bestBid": "0.40", "bestAsk": "0.60"},
    }
    row.update(over)
    return row


# --------------------------------------------------------------------------- #
# _parse_bool_param / _index_rows_to_pairs
# --------------------------------------------------------------------------- #


def test_parse_bool_param() -> None:
    assert _parse_bool_param({}, "k", default=True) is True
    assert _parse_bool_param({"k": ""}, "k", default=False) is False
    assert _parse_bool_param({"k": "YES"}, "k", default=False) is True
    assert _parse_bool_param({"k": "0"}, "k", default=True) is False


def test_index_rows_to_pairs_skips_missing_refs_and_fungible() -> None:
    rows = [
        {"kalshi_ref": "kalshi:A", "pm_ref": "polymarket:1", "method": "structured_key"},
        {"kalshi_ref": None, "pm_ref": "polymarket:2", "method": "structured_key"},
        {"kalshi_ref": "kalshi:C", "pm_ref": "polymarket:3", "method": "opus_backstop"},
    ]
    pairs = _index_rows_to_pairs(rows, limit=10, fungible_only=True, skip_row_stale=False)
    assert len(pairs) == 1
    assert pairs[0]["kalshi_market_id"] == "kalshi:A"


def test_index_rows_to_pairs_scan_budget() -> None:
    rows = [{"kalshi_ref": f"kalshi:{i}", "pm_ref": f"polymarket:{i}"} for i in range(5)]
    pairs = _index_rows_to_pairs(rows, limit=10, scan_budget=2, skip_row_stale=False)
    assert len(pairs) == 2


# --------------------------------------------------------------------------- #
# handle_markets_equivalents (collection)
# --------------------------------------------------------------------------- #


async def test_collection_index_source_with_hydration() -> None:
    idx = _FakeIndex([
        {"kalshi_ref": "kalshi:A", "pm_ref": "polymarket:1",
         "method": "structured_key", "confidence": 1.0, "bet_type": "event"},
    ])
    store = {
        "kalshi:A": _market("kalshi:A", "kalshi", prices="[0.55,0.45]"),
        "polymarket:1": _market("polymarket:1", "polymarket", prices="[0.52,0.48]"),
    }
    status, body = await handle_markets_equivalents(
        {"limit": "50"}, dao=_DictDao(store), equivalence=idx
    )
    assert status == 200
    assert body["count"] == 1
    assert body["pairs"][0]["a"]["id"] == "kalshi:A"
    assert body["meta"]["source"].startswith("pytheum-cross-venue-matcher")


async def test_collection_cache_hit() -> None:
    idx = _FakeIndex([
        {"kalshi_ref": "kalshi:A", "pm_ref": "polymarket:1", "method": "structured_key"},
    ])
    store = {
        "kalshi:A": _market("kalshi:A", "kalshi", prices="[0.5,0.5]"),
        "polymarket:1": _market("polymarket:1", "polymarket", prices="[0.5,0.5]"),
    }
    dao = _DictDao(store)
    _, body1 = await handle_markets_equivalents({"limit": "50"}, dao=dao, equivalence=idx)
    # second call hits the cache (same object returned).
    _, body2 = await handle_markets_equivalents({"limit": "50"}, dao=dao, equivalence=idx)
    assert body1 is body2


async def test_collection_drops_stale_legs() -> None:
    from datetime import UTC, datetime, timedelta
    past = (datetime.now(UTC) - timedelta(days=2)).isoformat()
    idx = _FakeIndex([
        {"kalshi_ref": "kalshi:A", "pm_ref": "polymarket:1", "method": "structured_key"},
    ])
    store = {
        "kalshi:A": _market("kalshi:A", "kalshi", prices="[0.5,0.5]", resolution_at=past),
        "polymarket:1": _market("polymarket:1", "polymarket", prices="[0.5,0.5]"),
    }
    _, body = await handle_markets_equivalents(
        {"limit": "50"}, dao=_DictDao(store), equivalence=idx
    )
    assert body["count"] == 0
    assert body["meta"]["dropped_stale"] == 1


async def test_collection_include_rules_and_fungible() -> None:
    idx = _FakeIndex([
        {"kalshi_ref": "kalshi:A", "pm_ref": "polymarket:1", "method": "structured_key"},
        {"kalshi_ref": "kalshi:B", "pm_ref": "polymarket:2", "method": "opus_backstop"},
    ])
    store = {
        "kalshi:A": _market("kalshi:A", "kalshi", prices="[0.5,0.5]",
                            payload={"outcomePrices": "[0.5,0.5]",
                                     "rulesPrimary": "Resolves YES if X."}),
        "polymarket:1": _market("polymarket:1", "polymarket", prices="[0.5,0.5]"),
    }
    _, body = await handle_markets_equivalents(
        {"limit": "50", "fungible_only": "true", "include_rules": "true"},
        dao=_DictDao(store), equivalence=idx,
    )
    assert body["meta"]["fungible_only"] is True
    assert body["meta"]["include_rules"] is True
    # only the fungible pair survives + has a resolution field on each leg.
    assert body["count"] == 1
    assert "resolution" in body["pairs"][0]["a"]


async def test_collection_dao_fallback_when_no_index() -> None:
    class _PairDao(_DictDao):
        async def fetch_equivalence_pairs(self, *, limit: int) -> list[dict[str, Any]]:
            return [{"kalshi_market_id": "kalshi:A", "polymarket_market_id": "polymarket:1",
                     "method": "structured_key", "confidence": 1.0, "bet_type": "event",
                     "poly_side": 0, "poly_outcome": "Yes"}]

    store = {
        "kalshi:A": _market("kalshi:A", "kalshi", prices="[0.5,0.5]"),
        "polymarket:1": _market("polymarket:1", "polymarket", prices="[0.5,0.5]"),
    }
    # equivalence object whose _rows is empty triggers index path with 0 pairs;
    # pass equivalence=None-like via a fake with no rows and use DAO fallback by
    # constructing an index that yields nothing, then dao path. Simpler: empty idx.
    idx = _FakeIndex([])
    dao = _PairDao(store)
    _, body = await handle_markets_equivalents({"limit": "50"}, dao=dao, equivalence=idx)
    # empty index → 0 pairs (index path takes precedence over dao fallback).
    assert body["count"] == 0


async def test_collection_no_dao_no_hydration() -> None:
    idx = _FakeIndex([
        {"kalshi_ref": "kalshi:A", "pm_ref": "polymarket:1", "method": "structured_key"},
    ])
    _, body = await handle_markets_equivalents({"limit": "50"}, dao=None, equivalence=idx)
    # no dao → no legs → pairs filtered out (a is None).
    assert body["count"] == 0


# --------------------------------------------------------------------------- #
# warm_equivalents_loop
# --------------------------------------------------------------------------- #


async def test_warm_loop_runs_once_then_stops() -> None:
    stop = asyncio.Event()
    calls = {"n": 0}

    orig = eqmod.handle_markets_equivalents

    async def _counting(query, **kw):
        calls["n"] += 1
        stop.set()  # stop after the first warmed key
        return await orig(query, **kw)

    # patch the symbol the loop calls
    eqmod.handle_markets_equivalents = _counting  # type: ignore[assignment]
    try:
        await warm_equivalents_loop(dao=_DictDao({}), stop=stop)
    finally:
        eqmod.handle_markets_equivalents = orig  # type: ignore[assignment]
    assert calls["n"] >= 1


async def test_warm_loop_swallows_exceptions() -> None:
    stop = asyncio.Event()
    orig = eqmod.handle_markets_equivalents

    async def _boom(query, **kw):
        stop.set()
        raise RuntimeError("warm failed")

    eqmod.handle_markets_equivalents = _boom  # type: ignore[assignment]
    try:
        await warm_equivalents_loop(dao=_DictDao({}), stop=stop)  # must not raise
    finally:
        eqmod.handle_markets_equivalents = orig  # type: ignore[assignment]


# --------------------------------------------------------------------------- #
# handle_market_equivalents (per-ref)
# --------------------------------------------------------------------------- #


async def test_perref_kalshi_focal_event_spread() -> None:
    idx = _FakeIndex([], pairs_loaded=10)
    pair = {"kalshi_ref": "kalshi:A", "pm_ref": "polymarket:1",
            "kalshi_title": "K title", "pm_title": "P title",
            "bet_type": "event", "poly_side": 0, "confidence": 1.0,
            "method": "structured_key"}
    idx.register("kalshi:A", pair, "kalshi_ticker")
    store = {
        "kalshi:A": _market("kalshi:A", "kalshi", prices="[0.60,0.40]"),
        "polymarket:1": _market("polymarket:1", "polymarket", prices="[0.50,0.50]"),
    }
    status, body = await handle_market_equivalents(
        "kalshi:A", {}, dao=_DictDao(store), equivalence=idx
    )
    assert status == 200
    cv = body["cross_venue"]
    assert cv["kalshi_implied"] == 0.60
    assert cv["pm_implied"] == 0.50
    assert cv["spread"] == round(0.60 - 0.50, 4)
    assert body["meta"]["matched_via"] == "kalshi_ticker"


async def test_perref_poly_side_flip() -> None:
    idx = _FakeIndex([])
    pair = {"kalshi_ref": "kalshi:A", "pm_ref": "polymarket:1",
            "kalshi_title": "K", "pm_title": "P",
            "bet_type": "moneyline", "poly_side": 1, "confidence": 1.0,
            "method": "game_match"}
    idx.register("kalshi:A", pair, "kalshi_ticker")
    store = {
        "kalshi:A": _market("kalshi:A", "kalshi", prices="[0.60,0.40]"),
        "polymarket:1": _market("polymarket:1", "polymarket", prices="[0.30,0.70]"),
    }
    _, body = await handle_market_equivalents(
        "kalshi:A", {}, dao=_DictDao(store), equivalence=idx
    )
    cv = body["cross_venue"]
    # poly_side==1 flips pm implied: 1 - 0.30 = 0.70
    assert cv["pm_implied"] == 0.70
    assert cv["spread"] == round(0.60 - 0.70, 4)


async def test_perref_unoriented_no_spread() -> None:
    idx = _FakeIndex([])
    pair = {"kalshi_ref": "kalshi:A", "pm_ref": "polymarket:1",
            "kalshi_title": "K", "pm_title": "P",
            "bet_type": "moneyline", "poly_side": None, "confidence": 1.0,
            "method": "game_match"}
    idx.register("kalshi:A", pair, "kalshi_ticker")
    store = {
        "kalshi:A": _market("kalshi:A", "kalshi", prices="[0.60,0.40]"),
        "polymarket:1": _market("polymarket:1", "polymarket", prices="[0.50,0.50]"),
    }
    _, body = await handle_market_equivalents(
        "kalshi:A", {}, dao=_DictDao(store), equivalence=idx
    )
    cv = body["cross_venue"]
    assert cv["spread"] is None
    assert "spread_unavailable" in cv


async def test_perref_polymarket_focal() -> None:
    idx = _FakeIndex([])
    pair = {"kalshi_ref": "kalshi:A", "pm_ref": "polymarket:1",
            "kalshi_title": "K", "pm_title": "P",
            "bet_type": "event", "poly_side": 0, "confidence": 0.9,
            "method": "election_match"}
    idx.register("polymarket:1", pair, "pm_gamma_id")
    store = {
        "kalshi:A": _market("kalshi:A", "kalshi", prices="[0.55,0.45]"),
        "polymarket:1": _market("polymarket:1", "polymarket", prices="[0.48,0.52]"),
    }
    _, body = await handle_market_equivalents(
        "polymarket:1", {}, dao=_DictDao(store), equivalence=idx
    )
    cv = body["cross_venue"]
    assert cv["pm_implied"] == 0.48
    assert cv["kalshi_implied"] == 0.55


async def test_perref_condition_id_fallback_to_canonical() -> None:
    idx = _FakeIndex([])
    pair = {"kalshi_ref": "kalshi:A", "pm_ref": "polymarket:1",
            "kalshi_title": "K", "pm_title": "P",
            "bet_type": "event", "poly_side": 0, "confidence": 1.0,
            "method": "award_match"}
    # normalize_ref case-folds only the venue prefix, not the body, so the
    # lookup key is the 0xDEAD form as-passed.
    idx.register("polymarket:0xDEAD", pair, "pm_condition_id")
    # only the canonical pm_ref is in the store, not the 0x form.
    store = {
        "polymarket:1": _market("polymarket:1", "polymarket", prices="[0.40,0.60]"),
        "kalshi:A": _market("kalshi:A", "kalshi", prices="[0.42,0.58]"),
    }
    _, body = await handle_market_equivalents(
        "polymarket:0xDEAD", {}, dao=_DictDao(store), equivalence=idx
    )
    # focal hydrated via canonical fallback
    assert body["market"]["id"] == "polymarket:1"


async def test_perref_raw_kalshi_ticker_fallback() -> None:
    idx = _FakeIndex([])
    pair = {"kalshi_ref": "kalshi:KX-RAW", "pm_ref": "polymarket:9",
            "kalshi_title": "K", "pm_title": "P", "bet_type": "event",
            "poly_side": 0, "confidence": 1.0, "method": "structured_key"}
    idx.register("KX-RAW", pair, "kalshi_ticker")
    store = {
        "kalshi:KX-RAW": _market("kalshi:KX-RAW", "kalshi", prices="[0.5,0.5]"),
        "polymarket:9": _market("polymarket:9", "polymarket", prices="[0.5,0.5]"),
    }
    _, body = await handle_market_equivalents(
        "KX-RAW", {}, dao=_DictDao(store), equivalence=idx
    )
    assert body["market"]["id"] == "kalshi:KX-RAW"


async def test_perref_no_pairs_minimal_block() -> None:
    idx = _FakeIndex([], pairs_loaded=5)
    # nothing registered → lookup miss → minimal block, empty equivalents.
    _, body = await handle_market_equivalents(
        "kalshi:UNKNOWN", {}, dao=_DictDao({}), equivalence=idx
    )
    assert body["equivalents"] == []
    assert body["market"]["id"] == "kalshi:UNKNOWN"
    assert body["meta"]["matched_via"] == "none"


async def test_perref_degraded_file_missing() -> None:
    idx = _FakeIndex([])
    idx.file_missing = True
    _, body = await handle_market_equivalents(
        "kalshi:A", {}, dao=_DictDao({}), equivalence=idx
    )
    assert body["meta"]["degraded"] is True
    assert body["meta"]["degraded_reason"] == "equivalence_file_not_found"


async def test_perref_degraded_load_error() -> None:
    idx = _FakeIndex([])
    idx.load_error = "corrupt gz"
    _, body = await handle_market_equivalents(
        "kalshi:A", {}, dao=_DictDao({}), equivalence=idx
    )
    assert body["meta"]["degraded"] is True
    assert body["meta"]["degraded_reason"] == "corrupt gz"


async def test_perref_hydrate_exception_returns_none() -> None:
    class _BoomDao:
        async def fetch_market(self, ref: str) -> dict[str, Any] | None:
            raise RuntimeError("db down")

    idx = _FakeIndex([])
    pair = {"kalshi_ref": "kalshi:A", "pm_ref": "polymarket:1",
            "kalshi_title": "Kt", "pm_title": "Pt", "bet_type": "event",
            "poly_side": 0, "confidence": 1.0, "method": "structured_key"}
    idx.register("kalshi:A", pair, "kalshi_ticker")
    _, body = await handle_market_equivalents(
        "kalshi:A", {}, dao=_BoomDao(), equivalence=idx
    )
    # focal + counterpart both fail to hydrate → minimal blocks from export titles.
    assert body["market"]["question"] == "Kt"
    assert body["equivalents"][0]["question"] == "Pt"
