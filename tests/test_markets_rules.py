"""Tests for GET /v1/markets/{ref}/rules.

Handler tests call handle_market_rules directly with a fake DAO and a fake
EquivalenceIndex (no real disk I/O).
"""
from __future__ import annotations

import pytest

from pytheum.api.markets_rules import handle_market_rules
from pytheum.equivalence.index import EquivalenceIndex

# ---------------------------------------------------------------------------
# Shared test fixtures
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
    "kalshi_title": "Will Test happen? (Kalshi)",
    "pm_title": "Will Test happen? (Polymarket)",
}


def _make_index(pairs: list[dict] | None = None, *, file_missing: bool = False) -> EquivalenceIndex:
    idx = EquivalenceIndex()
    if file_missing:
        idx.file_missing = True
        return idx
    idx.dataset_version = "2026-06-11T00:00:00Z"
    for row in ([_PAIR] if pairs is None else pairs):
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
    def __init__(self, store: dict | None = None) -> None:
        self._store: dict = store or {}

    async def fetch_market(self, ref: str) -> dict | None:
        return self._store.get(ref)


_KALSHI_ROW = {
    "id": "kalshi:KX-TEST-YES",
    "question": "Will Test happen? (Kalshi)",
    "venue": "kalshi",
    "status": "active",
    "volume_usd": 1000.0,
    "url": "https://kalshi.com/markets/kx-test-yes",
    "resolution_at": "2026-12-31T00:00:00+00:00",
    "payload": {
        "description": "This market resolves YES if Test happens before Dec 31 2026.",
        "bestBid": "0.63",
        "bestAsk": "0.67",
    },
}

_PM_ROW = {
    "id": "polymarket:12345",
    "question": "Will Test happen? (Polymarket)",
    "venue": "polymarket",
    "status": "active",
    "volume_usd": 50000.0,
    "url": "https://polymarket.com/event/will-test-happen",
    "resolution_at": "2026-12-31T12:00:00+00:00",
    "payload": {
        "description": "This market will resolve to YES if Test happens by December 31, 2026.",
        "outcomePrices": "[0.62, 0.38]",
        "bestBid": "0.61",
        "bestAsk": "0.63",
    },
}


# ---------------------------------------------------------------------------
# Core behaviour: pair with both rules present
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_both_rules_present():
    """When both focal and equivalent are in the store, both get resolution text."""
    dao = _SimpleDao({"kalshi:KX-TEST-YES": _KALSHI_ROW, "polymarket:12345": _PM_ROW})
    idx = _make_index()
    status, body = await handle_market_rules(
        "kalshi:KX-TEST-YES", {}, dao=dao, equivalence=idx
    )
    assert status == 200

    market = body["market"]
    assert market["id"] == "kalshi:KX-TEST-YES"
    assert market["venue"] == "kalshi"
    assert market["question"] == "Will Test happen? (Kalshi)"
    assert market["resolution"] is not None
    assert "resolves YES" in market["resolution"]
    assert market["resolution_at"] == "2026-12-31T00:00:00+00:00"
    assert market["url"] == "https://kalshi.com/markets/kx-test-yes"

    equiv = body["equivalent"]
    assert equiv is not None
    assert equiv["id"] == "polymarket:12345"
    assert equiv["venue"] == "polymarket"
    assert equiv["resolution"] is not None
    assert "resolve to YES" in equiv["resolution"]
    assert equiv["resolution_at"] == "2026-12-31T12:00:00+00:00"
    assert equiv["url"] == "https://polymarket.com/event/will-test-happen"

    meta = body["meta"]
    assert meta["matched_via"] == "kalshi_ticker"
    assert meta["pairs_loaded"] == 1


@pytest.mark.asyncio
async def test_pm_focal_both_rules():
    """Lookup from the PM side returns Kalshi as equivalent."""
    dao = _SimpleDao({"kalshi:KX-TEST-YES": _KALSHI_ROW, "polymarket:12345": _PM_ROW})
    idx = _make_index()
    status, body = await handle_market_rules(
        "polymarket:12345", {}, dao=dao, equivalence=idx
    )
    assert status == 200
    assert body["market"]["venue"] == "polymarket"
    equiv = body["equivalent"]
    assert equiv is not None
    assert equiv["venue"] == "kalshi"
    assert equiv["resolution"] is not None
    assert body["meta"]["matched_via"] == "pm_gamma_id"


# ---------------------------------------------------------------------------
# Focal-only: no equivalent in index
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_focal_only_no_equivalent():
    """When the market has no pair in the equivalence index, equivalent is null."""
    dao = _SimpleDao({"kalshi:KX-TEST-YES": _KALSHI_ROW})
    idx = _make_index(pairs=[])  # empty index
    status, body = await handle_market_rules(
        "kalshi:KX-TEST-YES", {}, dao=dao, equivalence=idx
    )
    assert status == 200
    assert body["market"]["id"] == "kalshi:KX-TEST-YES"
    assert body["equivalent"] is None
    assert body["comparison"]["same_deadline_day"] is None
    assert body["comparison"]["deadlines"]["kalshi"] is not None  # from store
    assert body["comparison"]["deadlines"]["polymarket"] is None
    assert body["comparison"]["confidence"] is None
    assert body["comparison"]["method"] is None


# ---------------------------------------------------------------------------
# Unknown ref: not in index, not in store
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_unknown_ref_returns_200_with_nulls():
    """An unknown ref returns 200 with minimal focal block and null equivalent."""
    dao = _SimpleDao()
    idx = _make_index()
    status, body = await handle_market_rules(
        "kalshi:KXDOESNOTEXIST", {}, dao=dao, equivalence=idx
    )
    assert status == 200
    assert body["market"]["id"] == "kalshi:KXDOESNOTEXIST"
    assert body["market"]["resolution"] is None
    assert body["market"]["resolution_at"] is None
    assert body["equivalent"] is None
    assert body["meta"]["matched_via"] == "none"


# ---------------------------------------------------------------------------
# Deadline comparison logic
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_same_deadline_day_true():
    """same_deadline_day is True when both sides resolve on the same calendar day."""
    kalshi_row = dict(_KALSHI_ROW, resolution_at="2026-12-31T00:00:00+00:00")
    pm_row = dict(_PM_ROW, resolution_at="2026-12-31T23:59:59+00:00")
    dao = _SimpleDao({"kalshi:KX-TEST-YES": kalshi_row, "polymarket:12345": pm_row})
    idx = _make_index()
    _, body = await handle_market_rules(
        "kalshi:KX-TEST-YES", {}, dao=dao, equivalence=idx
    )
    cmp = body["comparison"]
    assert cmp["deadlines"]["kalshi"] == "2026-12-31T00:00:00+00:00"
    assert cmp["deadlines"]["polymarket"] == "2026-12-31T23:59:59+00:00"
    assert cmp["same_deadline_day"] is True


@pytest.mark.asyncio
async def test_same_deadline_day_false():
    """same_deadline_day is False when the venues resolve on different calendar days."""
    kalshi_row = dict(_KALSHI_ROW, resolution_at="2026-12-31T00:00:00+00:00")
    pm_row = dict(_PM_ROW, resolution_at="2027-01-01T00:00:00+00:00")
    dao = _SimpleDao({"kalshi:KX-TEST-YES": kalshi_row, "polymarket:12345": pm_row})
    idx = _make_index()
    _, body = await handle_market_rules(
        "kalshi:KX-TEST-YES", {}, dao=dao, equivalence=idx
    )
    assert body["comparison"]["same_deadline_day"] is False


@pytest.mark.asyncio
async def test_same_deadline_day_null_when_either_missing():
    """same_deadline_day is None when either side has no resolution_at."""
    kalshi_row = dict(_KALSHI_ROW, resolution_at=None)
    pm_row = dict(_PM_ROW, resolution_at="2026-12-31T00:00:00+00:00")
    dao = _SimpleDao({"kalshi:KX-TEST-YES": kalshi_row, "polymarket:12345": pm_row})
    idx = _make_index()
    _, body = await handle_market_rules(
        "kalshi:KX-TEST-YES", {}, dao=dao, equivalence=idx
    )
    assert body["comparison"]["same_deadline_day"] is None


@pytest.mark.asyncio
async def test_deadline_comparison_from_pm_focal():
    """Deadline mapping is correct when the focal market is on Polymarket."""
    pm_row = dict(_PM_ROW, resolution_at="2026-11-15T00:00:00+00:00")
    kalshi_row = dict(_KALSHI_ROW, resolution_at="2026-11-15T00:00:00+00:00")
    dao = _SimpleDao({"polymarket:12345": pm_row, "kalshi:KX-TEST-YES": kalshi_row})
    idx = _make_index()
    _, body = await handle_market_rules(
        "polymarket:12345", {}, dao=dao, equivalence=idx
    )
    cmp = body["comparison"]
    assert cmp["deadlines"]["polymarket"] is not None
    assert cmp["deadlines"]["kalshi"] is not None
    assert cmp["same_deadline_day"] is True


# ---------------------------------------------------------------------------
# Export-title fallback when counterpart is not in store
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_export_title_used_when_counterpart_absent():
    """When equivalent isn't in the store, question comes from the export title."""
    dao = _SimpleDao({"kalshi:KX-TEST-YES": _KALSHI_ROW})  # no PM row
    idx = _make_index()
    _, body = await handle_market_rules(
        "kalshi:KX-TEST-YES", {}, dao=dao, equivalence=idx
    )
    equiv = body["equivalent"]
    assert equiv is not None
    assert equiv["question"] == "Will Test happen? (Polymarket)"
    assert equiv["resolution"] is None
    assert equiv["url"] is None
    assert equiv["resolution_at"] is None


# ---------------------------------------------------------------------------
# confidence + method from the pair
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_comparison_confidence_and_method():
    """comparison.confidence and .method come from the equivalence pair."""
    dao = _SimpleDao({"kalshi:KX-TEST-YES": _KALSHI_ROW, "polymarket:12345": _PM_ROW})
    idx = _make_index()
    _, body = await handle_market_rules(
        "kalshi:KX-TEST-YES", {}, dao=dao, equivalence=idx
    )
    cmp = body["comparison"]
    assert cmp["confidence"] == 1.0
    assert cmp["method"] == "blocked_deterministic"


# ---------------------------------------------------------------------------
# Degraded: missing equivalence file
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_missing_file_degrades_gracefully():
    """Missing equivalence file → 200 with meta.degraded, null equivalent."""
    dao = _SimpleDao({"kalshi:KX-TEST-YES": _KALSHI_ROW})
    idx = _make_index(file_missing=True)
    status, body = await handle_market_rules(
        "kalshi:KX-TEST-YES", {}, dao=dao, equivalence=idx
    )
    assert status == 200
    assert body["equivalent"] is None
    assert body["meta"]["degraded"] is True
    assert body["meta"]["degraded_reason"] == "equivalence_file_not_found"
    assert body["meta"]["pairs_loaded"] == 0


# ---------------------------------------------------------------------------
# Meta block present
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_meta_block_present():
    dao = _SimpleDao()
    idx = _make_index()
    _, body = await handle_market_rules(
        "kalshi:KX-TEST-YES", {}, dao=dao, equivalence=idx
    )
    meta = body["meta"]
    assert meta["pairs_loaded"] == 1
    assert meta["dataset_version"] == "2026-06-11T00:00:00Z"
    assert "matched_via" in meta
