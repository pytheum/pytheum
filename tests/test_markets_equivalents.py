"""Tests for GET /v1/markets/{ref}/equivalents.

Handler tests call handle_market_equivalents directly with a fake DAO and a
fake EquivalenceIndex (no real disk I/O).
"""
from __future__ import annotations

import gzip
import json
from pathlib import Path

import pytest

from pytheum.api.markets_equivalents import handle_market_equivalents
from pytheum.equivalence.index import EquivalenceIndex

# ---------------------------------------------------------------------------
# Fake helpers
# ---------------------------------------------------------------------------

_PAIR = {
    "kalshi_ref": "kalshi:KX-TEST-YES",
    "kalshi_ticker": "KX-TEST-YES",
    "pm_ref": "polymarket:12345",
    "pm_gamma_id": "12345",
    "pm_condition_id": "0xabc123",
    "pm_slug": "will-test-happen",
    "bet_type": "event",
    "slice": "curated",
    "method": "blocked_deterministic",
    "confidence": 1.0,
    "kalshi_title": "Will Test happen?",
    "pm_title": "Will Test happen?",
}


def _make_index(pairs: list[dict] | None = None, *, file_missing: bool = False) -> EquivalenceIndex:
    """Build an EquivalenceIndex in-memory without touching disk."""
    idx = EquivalenceIndex()
    if file_missing:
        idx.file_missing = True
        return idx
    idx.dataset_version = "2026-06-11T00:00:00Z"
    for row in (pairs or [_PAIR]):
        idx._rows.append(row)
        kt = row.get("kalshi_ticker")
        if kt:
            idx._by_kalshi_ticker.setdefault(kt, []).append(row)
        gid = row.get("pm_gamma_id")
        if gid is not None:
            idx._by_pm_gamma_id.setdefault(str(gid), []).append(row)
        cid = row.get("pm_condition_id")
        if cid:
            idx._by_pm_condition_id.setdefault(cid.lower(), []).append(row)
        slug = row.get("pm_slug")
        if slug:
            idx._by_pm_slug.setdefault(slug, []).append(row)
    return idx


class _SimpleDao:
    """Minimal DAO: returns canned rows for known refs."""

    def __init__(self, store: dict | None = None) -> None:
        self._store: dict = store or {}

    async def fetch_market(self, ref: str) -> dict | None:
        return self._store.get(ref)


_KALSHI_ROW = {
    "id": "kalshi:KX-TEST-YES",
    "question": "Will Test happen?",
    "venue": "kalshi",
    "status": "active",
    "volume_usd": 1000.0,
    "liquidity_usd": 500.0,
    "url": "https://kalshi.com/markets/kx-test-yes",
    "resolution_at": None,
    "payload": {"yes_price": 0.65, "bestBid": "0.63", "bestAsk": "0.67"},
}

_PM_ROW = {
    "id": "polymarket:12345",
    "question": "Will Test happen?",
    "venue": "polymarket",
    "status": "active",
    "volume_usd": 50000.0,
    "liquidity_usd": 20000.0,
    "url": "https://polymarket.com/event/will-test-happen",
    "resolution_at": None,
    "payload": {
        "outcomePrices": "[0.62, 0.38]",
        "bestBid": "0.61",
        "bestAsk": "0.63",
    },
}


# ---------------------------------------------------------------------------
# Lookup by each key type
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_lookup_by_kalshi_ref():
    dao = _SimpleDao({"kalshi:KX-TEST-YES": _KALSHI_ROW, "polymarket:12345": _PM_ROW})
    idx = _make_index()
    status, body = await handle_market_equivalents(
        "kalshi:KX-TEST-YES", {}, dao=dao, equivalence=idx
    )
    assert status == 200
    assert body["market"]["id"] == "kalshi:KX-TEST-YES"
    assert body["market"]["venue"] == "kalshi"
    assert len(body["equivalents"]) == 1
    eq = body["equivalents"][0]
    assert eq["id"] == "polymarket:12345"
    assert eq["venue"] == "polymarket"
    assert eq["bet_type"] == "event"
    assert eq["confidence"] == 1.0
    assert body["meta"]["matched_via"] == "kalshi_ticker"
    assert body["meta"]["pairs_loaded"] == 1


@pytest.mark.asyncio
async def test_lookup_by_pm_gamma_id():
    dao = _SimpleDao({"polymarket:12345": _PM_ROW, "kalshi:KX-TEST-YES": _KALSHI_ROW})
    idx = _make_index()
    status, body = await handle_market_equivalents(
        "polymarket:12345", {}, dao=dao, equivalence=idx
    )
    assert status == 200
    assert body["market"]["venue"] == "polymarket"
    assert len(body["equivalents"]) == 1
    assert body["equivalents"][0]["id"] == "kalshi:KX-TEST-YES"
    assert body["equivalents"][0]["venue"] == "kalshi"
    assert body["meta"]["matched_via"] == "pm_gamma_id"


@pytest.mark.asyncio
async def test_lookup_by_pm_condition_id():
    dao = _SimpleDao({"polymarket:12345": _PM_ROW, "kalshi:KX-TEST-YES": _KALSHI_ROW})
    idx = _make_index()
    status, body = await handle_market_equivalents(
        "polymarket:0xabc123", {}, dao=dao, equivalence=idx
    )
    assert status == 200
    assert body["meta"]["matched_via"] == "pm_condition_id"
    assert len(body["equivalents"]) == 1
    assert body["equivalents"][0]["venue"] == "kalshi"


@pytest.mark.asyncio
async def test_lookup_by_pm_slug():
    dao = _SimpleDao({"polymarket:12345": _PM_ROW, "kalshi:KX-TEST-YES": _KALSHI_ROW})
    idx = _make_index()
    status, body = await handle_market_equivalents(
        "polymarket:will-test-happen", {}, dao=dao, equivalence=idx
    )
    assert status == 200
    assert body["meta"]["matched_via"] == "pm_slug"
    assert len(body["equivalents"]) == 1
    assert body["equivalents"][0]["venue"] == "kalshi"


@pytest.mark.asyncio
async def test_lookup_by_raw_ticker():
    """Bare ticker without venue prefix — looked up as kalshi_ticker."""
    dao = _SimpleDao({"kalshi:KX-TEST-YES": _KALSHI_ROW, "polymarket:12345": _PM_ROW})
    idx = _make_index()
    status, body = await handle_market_equivalents(
        "KX-TEST-YES", {}, dao=dao, equivalence=idx
    )
    assert status == 200
    assert body["meta"]["matched_via"] == "kalshi_ticker"
    assert len(body["equivalents"]) == 1


# ---------------------------------------------------------------------------
# Unknown ref -> empty equivalents + 200
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_unknown_ref_returns_empty_equivalents_200():
    dao = _SimpleDao()
    idx = _make_index()
    status, body = await handle_market_equivalents(
        "kalshi:KXDOESNOTEXIST", {}, dao=dao, equivalence=idx
    )
    assert status == 200
    assert body["equivalents"] == []
    assert body["cross_venue"] == {}
    assert body["meta"]["matched_via"] == "none"


@pytest.mark.asyncio
async def test_unknown_pm_ref_returns_empty_200():
    dao = _SimpleDao()
    idx = _make_index()
    status, body = await handle_market_equivalents(
        "polymarket:99999", {}, dao=dao, equivalence=idx
    )
    assert status == 200
    assert body["equivalents"] == []


# ---------------------------------------------------------------------------
# Missing file degrades gracefully
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_missing_file_degrades_to_empty_results():
    """When the equivalence file is absent the handler returns 200 with an
    empty equivalents list and a meta.degraded flag rather than crashing."""
    dao = _SimpleDao()
    idx = _make_index(file_missing=True)
    status, body = await handle_market_equivalents(
        "kalshi:KX-TEST-YES", {}, dao=dao, equivalence=idx
    )
    assert status == 200
    assert body["equivalents"] == []
    assert body["meta"]["degraded"] is True
    assert body["meta"]["degraded_reason"] == "equivalence_file_not_found"
    assert body["meta"]["pairs_loaded"] == 0


# ---------------------------------------------------------------------------
# Spread computation
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_spread_computed_when_both_sides_priced():
    """cross_venue.spread = kalshi_implied - pm_implied when both are available."""
    kalshi_row = dict(_KALSHI_ROW, payload={
        "outcomePrices": "[0.70, 0.30]",
        "bestBid": "0.69", "bestAsk": "0.71",
    })
    pm_row = dict(_PM_ROW, payload={
        "outcomePrices": "[0.65, 0.35]",
        "bestBid": "0.64", "bestAsk": "0.66",
    })
    dao = _SimpleDao({"kalshi:KX-TEST-YES": kalshi_row, "polymarket:12345": pm_row})
    idx = _make_index()
    _, body = await handle_market_equivalents(
        "kalshi:KX-TEST-YES", {}, dao=dao, equivalence=idx
    )
    cv = body["cross_venue"]
    assert "kalshi_implied" in cv
    assert "pm_implied" in cv
    assert "spread" in cv
    assert abs(cv["spread"] - (cv["kalshi_implied"] - cv["pm_implied"])) < 1e-6


@pytest.mark.asyncio
async def test_spread_absent_when_counterpart_not_in_store():
    """If the counterpart isn't in the store, implied_yes is None and spread
    is omitted from cross_venue."""
    dao = _SimpleDao({"kalshi:KX-TEST-YES": _KALSHI_ROW})  # no PM row
    idx = _make_index()
    _, body = await handle_market_equivalents(
        "kalshi:KX-TEST-YES", {}, dao=dao, equivalence=idx
    )
    assert body["equivalents"][0]["implied_yes"] is None
    assert "spread" not in body["cross_venue"]


# ---------------------------------------------------------------------------
# Export-only fallback (counterpart not in store but export title populated)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_export_title_used_when_counterpart_not_in_store():
    dao = _SimpleDao({"kalshi:KX-TEST-YES": _KALSHI_ROW})
    idx = _make_index()
    _, body = await handle_market_equivalents(
        "kalshi:KX-TEST-YES", {}, dao=dao, equivalence=idx
    )
    eq = body["equivalents"][0]
    assert eq["question"] == "Will Test happen?"  # from export pm_title
    assert eq["volume_usd"] is None
    assert eq["url"] is None


# ---------------------------------------------------------------------------
# Meta block
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_meta_block_present():
    dao = _SimpleDao()
    idx = _make_index()
    _, body = await handle_market_equivalents(
        "kalshi:KX-TEST-YES", {}, dao=dao, equivalence=idx
    )
    meta = body["meta"]
    assert meta["pairs_loaded"] == 1
    assert meta["dataset_version"] == "2026-06-11T00:00:00Z"
    assert "matched_via" in meta


# ---------------------------------------------------------------------------
# EquivalenceIndex.load — fault-tolerant with missing file (via tmp_path)
# ---------------------------------------------------------------------------

def test_index_load_missing_file(tmp_path: Path):
    """EquivalenceIndex.load with a non-existent path returns an empty index
    with file_missing=True rather than raising."""
    idx = EquivalenceIndex.load(tmp_path / "nonexistent.jsonl.gz")
    assert idx.file_missing is True
    assert idx.pairs_loaded == 0
    assert idx.dataset_version is None


def test_index_load_from_jsonl_gz(tmp_path: Path):
    """EquivalenceIndex.load correctly ingests a real .jsonl.gz file."""
    p = tmp_path / "test.jsonl.gz"
    with gzip.open(p, "wt") as f:
        f.write(json.dumps(_PAIR) + "\n")
    idx = EquivalenceIndex.load(p)
    assert idx.file_missing is False
    assert idx.pairs_loaded == 1
    rows, via = idx.lookup("kalshi:KX-TEST-YES")
    assert len(rows) == 1 and via == "kalshi_ticker"
    rows, via = idx.lookup("polymarket:12345")
    assert len(rows) == 1 and via == "pm_gamma_id"
    rows, via = idx.lookup("polymarket:0xabc123")
    assert len(rows) == 1 and via == "pm_condition_id"
    rows, via = idx.lookup("polymarket:will-test-happen")
    assert len(rows) == 1 and via == "pm_slug"


def test_index_lookup_empty_when_unknown():
    idx = _make_index()
    rows, via = idx.lookup("kalshi:KXNONE")
    assert rows == [] and via == "none"
    rows, via = idx.lookup("polymarket:99999")
    assert rows == [] and via == "none"


def test_index_lookup_case_insensitive_condition_id():
    """pm_condition_id lookup is case-insensitive (0xABC123 == 0xabc123)."""
    idx = _make_index()
    rows, via = idx.lookup("polymarket:0xABC123")
    assert len(rows) == 1 and via == "pm_condition_id"
