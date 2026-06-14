"""Tests for GET /v1/markets/matched and EquivalenceIndex.browse().

Handler tests call handle_markets_matched directly with a fake DAO and a fake
EquivalenceIndex (no real disk I/O).
"""
from __future__ import annotations

import asyncio

import pytest

from pytheum.api.markets_matched import handle_markets_matched
from pytheum.equivalence.index import EquivalenceIndex

# ---------------------------------------------------------------------------
# Fake pair rows
# ---------------------------------------------------------------------------

_MONEYLINE = {
    "kalshi_ref": "kalshi:KX-NBA-LAL-BOS",
    "kalshi_ticker": "KX-NBA-LAL-BOS",
    "pm_ref": "polymarket:10001",
    "pm_gamma_id": "10001",
    "pm_slug": "lakers-celtics-winner",
    "bet_type": "moneyline",
    "slice": "structured",
    "method": "structured_key",
    "confidence": 1.0,
    "kalshi_title": "Will the Lakers beat the Celtics?",
    "pm_title": "Lakers vs Celtics winner",
}

_TOTAL = {
    "kalshi_ref": "kalshi:KX-NFL-TOTAL-KC",
    "kalshi_ticker": "KX-NFL-TOTAL-KC",
    "pm_ref": "polymarket:10002",
    "pm_gamma_id": "10002",
    "pm_slug": "chiefs-total",
    "bet_type": "total",
    "slice": "structured",
    "method": "structured_key",
    "confidence": 1.0,
    "kalshi_title": "Will the Chiefs total be over 48.5?",
    "pm_title": "Chiefs vs Raiders total over 48.5",
}

_EVENT = {
    "kalshi_ref": "kalshi:KX-ELECTION-2026",
    "kalshi_ticker": "KX-ELECTION-2026",
    "pm_ref": "polymarket:10003",
    "pm_gamma_id": "10003",
    "pm_slug": "us-election-2026",
    "bet_type": "event",
    "slice": "curated",
    "method": "blocked_deterministic",
    "confidence": 1.0,
    "kalshi_title": "Will Democrats win the 2026 Senate?",
    "pm_title": "Democrats win 2026 Senate?",
}

_TENNIS = {
    "kalshi_ref": "kalshi:KX-TENNIS-AO-1",
    "kalshi_ticker": "KX-TENNIS-AO-1",
    "pm_ref": "polymarket:10004",
    "pm_gamma_id": "10004",
    "pm_slug": "djokovic-sinner-ao",
    "bet_type": "tennis_ml",
    "slice": "structured",
    "method": "structured_key",
    "confidence": 1.0,
    "kalshi_title": "Will Djokovic beat Sinner at AO?",
    "pm_title": "Djokovic vs Sinner AO winner",
}


_ALL_PAIRS = [_MONEYLINE, _TOTAL, _EVENT, _TENNIS]


def _make_index(
    pairs: list[dict] | None = None, *, file_missing: bool = False
) -> EquivalenceIndex:
    """Build an EquivalenceIndex in-memory without touching disk."""
    idx = EquivalenceIndex()
    if file_missing:
        idx.file_missing = True
        return idx
    idx.dataset_version = "2026-06-11T00:00:00Z"
    for row in (pairs if pairs is not None else _ALL_PAIRS):
        idx._rows.append(row)
        kt = row.get("kalshi_ticker")
        if kt:
            idx._by_kalshi_ticker.setdefault(kt, []).append(row)
        gid = row.get("pm_gamma_id")
        if gid is not None:
            idx._by_pm_gamma_id.setdefault(str(gid), []).append(row)
        slug = row.get("pm_slug")
        if slug:
            idx._by_pm_slug.setdefault(slug, []).append(row)
    return idx


class _SimpleDao:
    """Minimal DAO: returns canned rows for known refs.

    Implements fetch_markets_by_ids (batch) — the method used by
    handle_markets_matched after the N+1 fix.
    """

    def __init__(self, store: dict | None = None) -> None:
        self._store: dict = store or {}

    async def fetch_market(self, ref: str) -> dict | None:
        return self._store.get(ref)

    async def fetch_markets_by_ids(self, ids: list[str]) -> list[dict]:
        return [self._store[ref] for ref in ids if ref in self._store]


_KALSHI_ROW = {
    "id": "kalshi:KX-NBA-LAL-BOS",
    "question": "Will the Lakers beat the Celtics?",
    "venue": "kalshi",
    "status": "active",
    "volume_usd": 5000.0,
    "url": "https://kalshi.com/markets/kx-nba-lal-bos",
    "resolution_at": None,
    "payload": {"outcomePrices": "[0.55, 0.45]", "bestBid": "0.54", "bestAsk": "0.56"},
}

_PM_ROW = {
    "id": "polymarket:10001",
    "question": "Lakers vs Celtics winner",
    "venue": "polymarket",
    "status": "active",
    "volume_usd": 80000.0,
    "url": "https://polymarket.com/event/lakers-celtics",
    "resolution_at": None,
    "payload": {"outcomePrices": "[0.52, 0.48]", "bestBid": "0.51", "bestAsk": "0.53"},
}


# ---------------------------------------------------------------------------
# EquivalenceIndex.browse() unit tests
# ---------------------------------------------------------------------------


def test_browse_returns_all_rows_no_filter():
    idx = _make_index()
    rows, total = idx.browse()
    assert total == 4
    assert len(rows) == 4


def test_browse_bet_type_filter_moneyline():
    idx = _make_index()
    rows, total = idx.browse(bet_types={"moneyline"})
    assert total == 1
    assert all(r["bet_type"] == "moneyline" for r in rows)


def test_browse_sports_group_via_direct_set():
    """Browse with the expanded sports group should include moneyline+total+tennis."""
    idx = _make_index()
    sports_set = EquivalenceIndex.BET_TYPE_GROUPS["sports"]
    rows, total = idx.browse(bet_types=sports_set)
    returned_types = {r["bet_type"] for r in rows}
    assert "moneyline" in returned_types
    assert "total" in returned_types
    assert "tennis_ml" in returned_types
    assert "event" not in returned_types
    assert total == 3


def test_browse_query_substr_case_insensitive():
    idx = _make_index()
    rows, total = idx.browse(query_substr="LAKERS")
    assert total == 1
    assert rows[0]["bet_type"] == "moneyline"


def test_browse_query_substr_pm_title():
    idx = _make_index()
    rows, total = idx.browse(query_substr="senate")
    assert total == 1
    assert rows[0]["bet_type"] == "event"


def test_browse_pagination_limit_offset():
    idx = _make_index()
    rows1, total = idx.browse(limit=2, offset=0)
    rows2, _ = idx.browse(limit=2, offset=2)
    assert total == 4
    assert len(rows1) == 2
    assert len(rows2) == 2
    all_ids = [r["kalshi_ticker"] for r in rows1 + rows2]
    assert len(set(all_ids)) == 4  # no duplicates


def test_browse_no_match_returns_empty():
    idx = _make_index()
    rows, total = idx.browse(query_substr="zzz_no_such_text_xyz")
    assert total == 0
    assert rows == []


def test_browse_combined_filter():
    """bet_types + query_substr both active."""
    idx = _make_index()
    rows, total = idx.browse(bet_types={"moneyline"}, query_substr="Lakers")
    assert total == 1
    assert rows[0]["bet_type"] == "moneyline"


def test_bet_types_available():
    idx = _make_index()
    avail = idx.bet_types_available
    assert "moneyline" in avail
    assert "total" in avail
    assert "event" in avail
    assert "tennis_ml" in avail
    assert avail == sorted(avail)  # must be sorted


def test_bet_types_available_empty_index():
    idx = _make_index(pairs=[])
    assert idx.bet_types_available == []


# ---------------------------------------------------------------------------
# Handler tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_handler_returns_all_pairs():
    dao = _SimpleDao()
    idx = _make_index()
    status, body = await handle_markets_matched({}, dao=dao, equivalence=idx)
    assert status == 200
    assert len(body["pairs"]) == 4
    assert body["total"] == 4


@pytest.mark.asyncio
async def test_handler_bet_type_filter_sports_group():
    """Passing bet_type='sports' should expand the group and return sports pairs."""
    dao = _SimpleDao()
    idx = _make_index()
    status, body = await handle_markets_matched(
        {"bet_type": "sports"}, dao=dao, equivalence=idx
    )
    assert status == 200
    returned_types = {p["bet_type"] for p in body["pairs"]}
    assert "moneyline" in returned_types
    assert "event" not in returned_types
    assert body["total"] == 3


@pytest.mark.asyncio
async def test_handler_bet_type_specific():
    dao = _SimpleDao()
    idx = _make_index()
    status, body = await handle_markets_matched(
        {"bet_type": "event"}, dao=dao, equivalence=idx
    )
    assert status == 200
    assert body["total"] == 1
    assert body["pairs"][0]["bet_type"] == "event"


@pytest.mark.asyncio
async def test_handler_query_substr_filter():
    dao = _SimpleDao()
    idx = _make_index()
    status, body = await handle_markets_matched(
        {"q": "celtics"}, dao=dao, equivalence=idx
    )
    assert status == 200
    assert body["total"] == 1
    assert body["pairs"][0]["bet_type"] == "moneyline"


@pytest.mark.asyncio
async def test_handler_pagination():
    dao = _SimpleDao()
    idx = _make_index()
    status, body = await handle_markets_matched(
        {"limit": "2", "offset": "0"}, dao=dao, equivalence=idx
    )
    assert status == 200
    assert len(body["pairs"]) == 2
    assert body["total"] == 4


@pytest.mark.asyncio
async def test_handler_meta_bet_types_available():
    dao = _SimpleDao()
    idx = _make_index()
    _, body = await handle_markets_matched({}, dao=dao, equivalence=idx)
    meta = body["meta"]
    assert "bet_types_available" in meta
    assert "moneyline" in meta["bet_types_available"]
    assert "event" in meta["bet_types_available"]
    assert meta["pairs_loaded"] == 4


@pytest.mark.asyncio
async def test_handler_response_shape():
    """Each pair must have kalshi, polymarket, bet_type, confidence, method,
    and cross_venue keys."""
    dao = _SimpleDao()
    idx = _make_index()
    _, body = await handle_markets_matched({}, dao=dao, equivalence=idx)
    for pair in body["pairs"]:
        assert "kalshi" in pair
        assert "polymarket" in pair
        assert "bet_type" in pair
        assert "confidence" in pair
        assert "method" in pair
        assert "cross_venue" in pair
        for side in ("kalshi", "polymarket"):
            assert "id" in pair[side]
            assert "question" in pair[side]


@pytest.mark.asyncio
async def test_handler_hydrates_from_store():
    """When the DAO has a row, implied_yes and volume are populated."""
    dao = _SimpleDao({"kalshi:KX-NBA-LAL-BOS": _KALSHI_ROW, "polymarket:10001": _PM_ROW})
    idx = _make_index()
    _, body = await handle_markets_matched(
        {"bet_type": "moneyline"}, dao=dao, equivalence=idx
    )
    pair = body["pairs"][0]
    assert pair["kalshi"]["volume_usd"] == 5000.0
    assert pair["polymarket"]["volume_usd"] == 80000.0
    assert pair["kalshi"]["implied_yes"] is not None
    assert pair["polymarket"]["implied_yes"] is not None
    cv = pair["cross_venue"]
    assert "kalshi_implied" in cv
    assert "pm_implied" in cv
    assert "spread" in cv


@pytest.mark.asyncio
async def test_handler_nulls_when_not_in_store():
    """Pairs not in the store get null price fields, not errors."""
    dao = _SimpleDao()
    idx = _make_index()
    _, body = await handle_markets_matched(
        {"bet_type": "moneyline"}, dao=dao, equivalence=idx
    )
    pair = body["pairs"][0]
    assert pair["kalshi"]["implied_yes"] is None
    assert pair["kalshi"]["volume_usd"] is None
    assert pair["cross_venue"] == {}


@pytest.mark.asyncio
async def test_handler_empty_file_degrades_gracefully():
    """Missing equivalence file -> 200 with empty pairs and meta.degraded."""
    dao = _SimpleDao()
    idx = _make_index(file_missing=True)
    status, body = await handle_markets_matched({}, dao=dao, equivalence=idx)
    assert status == 200
    assert body["pairs"] == []
    assert body["total"] == 0
    assert body["meta"]["degraded"] is True
    assert body["meta"]["degraded_reason"] == "equivalence_file_not_found"


@pytest.mark.asyncio
async def test_handler_meta_filter_echo():
    """The meta.filter block echoes back the applied filter params."""
    dao = _SimpleDao()
    idx = _make_index()
    _, body = await handle_markets_matched(
        {"bet_type": "moneyline", "q": "lakers", "limit": "10", "offset": "0"},
        dao=dao,
        equivalence=idx,
    )
    filt = body["meta"]["filter"]
    assert filt["bet_type"] == "moneyline"
    assert filt["q"] == "lakers"
    assert filt["limit"] == 10
    assert filt["offset"] == 0


# ---------------------------------------------------------------------------
# sort_by tests
# ---------------------------------------------------------------------------

_SPREAD_LARGE_ROW = {
    "kalshi_ref": "kalshi:KX-A",
    "kalshi_ticker": "KX-A",
    "pm_ref": "polymarket:20001",
    "pm_gamma_id": "20001",
    "pm_slug": "pair-a",
    "bet_type": "moneyline",
    "method": "structured_key",
    "confidence": 0.9,
    "kalshi_title": "Pair A kalshi",
    "pm_title": "Pair A pm",
}

_SPREAD_SMALL_ROW = {
    "kalshi_ref": "kalshi:KX-B",
    "kalshi_ticker": "KX-B",
    "pm_ref": "polymarket:20002",
    "pm_gamma_id": "20002",
    "pm_slug": "pair-b",
    "bet_type": "moneyline",
    "method": "structured_key",
    "confidence": 1.0,
    "kalshi_title": "Pair B kalshi",
    "pm_title": "Pair B pm",
}

_SPREAD_NULL_PM_ROW = {
    "kalshi_ref": "kalshi:KX-C",
    "kalshi_ticker": "KX-C",
    "pm_ref": "polymarket:20003",
    "pm_gamma_id": "20003",
    "pm_slug": "pair-c",
    "bet_type": "moneyline",
    "method": "structured_key",
    "confidence": 1.0,
    "kalshi_title": "Pair C kalshi — pm not hydrated",
    "pm_title": "Pair C pm",
}

_SPREAD_NULL_BOTH_ROW = {
    "kalshi_ref": "kalshi:KX-D",
    "kalshi_ticker": "KX-D",
    "pm_ref": "polymarket:20004",
    "pm_gamma_id": "20004",
    "pm_slug": "pair-d",
    "bet_type": "total",
    "method": "structured_key",
    "confidence": 0.8,
    "kalshi_title": "Pair D — neither side hydrated",
    "pm_title": "Pair D pm",
}

_SPREAD_STORE: dict = {
    "kalshi:KX-A": {
        "id": "kalshi:KX-A",
        "question": "Pair A kalshi",
        "venue": "kalshi",
        "volume_usd": 1000.0,
        "url": None,
        "payload": {"outcomePrices": "[0.70, 0.30]"},
    },
    "polymarket:20001": {
        "id": "polymarket:20001",
        "question": "Pair A pm",
        "venue": "polymarket",
        "volume_usd": 5000.0,
        "url": None,
        "payload": {"outcomePrices": "[0.50, 0.50]"},
    },
    "kalshi:KX-B": {
        "id": "kalshi:KX-B",
        "question": "Pair B kalshi",
        "venue": "kalshi",
        "volume_usd": 9000.0,
        "url": None,
        "payload": {"outcomePrices": "[0.60, 0.40]"},
    },
    "polymarket:20002": {
        "id": "polymarket:20002",
        "question": "Pair B pm",
        "venue": "polymarket",
        "volume_usd": 2000.0,
        "url": None,
        "payload": {"outcomePrices": "[0.55, 0.45]"},
    },
    "kalshi:KX-C": {
        "id": "kalshi:KX-C",
        "question": "Pair C kalshi",
        "venue": "kalshi",
        "volume_usd": 3000.0,
        "url": None,
        "payload": {"outcomePrices": "[0.80, 0.20]"},
    },
}

_SPREAD_PAIRS = [_SPREAD_LARGE_ROW, _SPREAD_SMALL_ROW, _SPREAD_NULL_PM_ROW, _SPREAD_NULL_BOTH_ROW]


def _make_spread_index() -> EquivalenceIndex:
    idx = EquivalenceIndex()
    idx.dataset_version = "2026-06-12T00:00:00Z"
    for row in _SPREAD_PAIRS:
        idx._rows.append(row)
        kt = row.get("kalshi_ticker")
        if kt:
            idx._by_kalshi_ticker.setdefault(kt, []).append(row)
        gid = row.get("pm_gamma_id")
        if gid is not None:
            idx._by_pm_gamma_id.setdefault(str(gid), []).append(row)
        slug = row.get("pm_slug")
        if slug:
            idx._by_pm_slug.setdefault(slug, []).append(row)
    return idx


@pytest.mark.asyncio
async def test_sort_spread_ordering_descending():
    """sort_by=spread → largest |kalshi_implied - pm_implied| comes first."""
    dao = _SimpleDao(_SPREAD_STORE)
    idx = _make_spread_index()
    _, body = await handle_markets_matched(
        {"sort_by": "spread"}, dao=dao, equivalence=idx
    )
    pairs = body["pairs"]
    hydrated = [
        p for p in pairs
        if p["cross_venue"].get("kalshi_implied") is not None
        and p["cross_venue"].get("pm_implied") is not None
    ]
    assert len(hydrated) >= 2
    spreads = [abs(p["cross_venue"]["kalshi_implied"] - p["cross_venue"]["pm_implied"])
               for p in hydrated]
    assert spreads == sorted(spreads, reverse=True), "hydrated pairs not sorted by spread desc"
    tickers = [p["kalshi"]["id"] for p in pairs]
    assert tickers.index("kalshi:KX-A") < tickers.index("kalshi:KX-B")


@pytest.mark.asyncio
async def test_sort_spread_nulls_last():
    """sort_by=spread → pairs with missing implied prices sort after hydrated ones."""
    dao = _SimpleDao(_SPREAD_STORE)
    idx = _make_spread_index()
    _, body = await handle_markets_matched(
        {"sort_by": "spread"}, dao=dao, equivalence=idx
    )
    pairs = body["pairs"]
    tickers = [p["kalshi"]["id"] for p in pairs]
    assert tickers.index("kalshi:KX-A") < tickers.index("kalshi:KX-C")
    assert tickers.index("kalshi:KX-B") < tickers.index("kalshi:KX-C")
    assert tickers.index("kalshi:KX-A") < tickers.index("kalshi:KX-D")
    assert tickers.index("kalshi:KX-B") < tickers.index("kalshi:KX-D")


@pytest.mark.asyncio
async def test_sort_spread_never_invents_spread_from_nulls():
    """Pairs with missing sides must have empty or null spread in cross_venue."""
    dao = _SimpleDao(_SPREAD_STORE)
    idx = _make_spread_index()
    _, body = await handle_markets_matched(
        {"sort_by": "spread"}, dao=dao, equivalence=idx
    )
    for pair in body["pairs"]:
        cv = pair["cross_venue"]
        ki = cv.get("kalshi_implied")
        pi = cv.get("pm_implied")
        if ki is None or pi is None:
            assert "spread" not in cv or cv.get("spread") is None


@pytest.mark.asyncio
async def test_sort_confidence():
    """sort_by=confidence → pairs sorted by confidence desc."""
    dao = _SimpleDao()
    idx = _make_index()
    low_conf = {
        **_MONEYLINE,
        "kalshi_ticker": "KX-LOW",
        "kalshi_ref": "kalshi:KX-LOW",
        "pm_gamma_id": "19999",
        "pm_ref": "polymarket:19999",
        "confidence": 0.5,
    }
    idx._rows.append(low_conf)
    _, body = await handle_markets_matched(
        {"sort_by": "confidence"}, dao=dao, equivalence=idx
    )
    pairs = body["pairs"]
    confs = [p["confidence"] for p in pairs]
    assert confs == sorted(confs, reverse=True)


@pytest.mark.asyncio
async def test_sort_invalid_falls_back_to_volume():
    """Unknown sort_by silently falls back to volume sort (never crashes)."""
    dao = _SimpleDao({"kalshi:KX-NBA-LAL-BOS": _KALSHI_ROW, "polymarket:10001": _PM_ROW})
    idx = _make_index()
    status, body = await handle_markets_matched(
        {"sort_by": "totally_invalid"}, dao=dao, equivalence=idx
    )
    assert status == 200
    assert len(body["pairs"]) == 4
    assert body["meta"]["filter"]["sort_by"] == "volume"


@pytest.mark.asyncio
async def test_sort_by_echoed_in_meta_filter():
    """sort_by is echoed in meta.filter regardless of value."""
    dao = _SimpleDao()
    idx = _make_index()
    _, body = await handle_markets_matched(
        {"sort_by": "spread"}, dao=dao, equivalence=idx
    )
    assert body["meta"]["filter"]["sort_by"] == "spread"


@pytest.mark.asyncio
async def test_sort_default_is_volume():
    """Omitting sort_by should default to volume mode, echoed in meta.filter."""
    dao = _SimpleDao()
    idx = _make_index()
    _, body = await handle_markets_matched({}, dao=dao, equivalence=idx)
    assert body["meta"]["filter"]["sort_by"] == "volume"


# ---------------------------------------------------------------------------
# Concurrency smoke test
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_concurrent_matched_requests_complete_in_parallel():
    """Two concurrent handle_markets_matched calls with a 50ms batch-DAO stub
    should complete in ~50ms (parallel) not ~100ms (serial)."""
    import time

    class _SlowBatchDao:
        async def fetch_markets_by_ids(self, ids: list[str]) -> list[dict]:
            await asyncio.sleep(0.05)
            return []

    dao = _SlowBatchDao()
    idx = _make_index()

    t0 = time.monotonic()
    await asyncio.gather(
        handle_markets_matched({}, dao=dao, equivalence=idx),
        handle_markets_matched({}, dao=dao, equivalence=idx),
    )
    elapsed = time.monotonic() - t0

    assert elapsed < 0.090, (
        f"Requests appear to have run serially: elapsed={elapsed:.3f}s "
        f"(expected <0.090s for parallel 50ms stubs)"
    )


@pytest.mark.asyncio
async def test_handler_ranks_live_pairs_first_over_higher_volume_settled():
    """Regression (matched-pairs live-first): settled markets
    retain lifetime volume, so a pure volume sort surfaces dead pairs on top. A
    live pair (both legs active) must outrank a higher-volume settled pair, and
    each pair must carry is_live."""
    # settled pair with HUGE lifetime volume; live pair with small volume
    settled_k = {**_KALSHI_ROW, "id": "kalshi:KX-OLD", "status": "settled",
                 "volume_usd": 5_000_000.0}
    settled_pm = {**_PM_ROW, "id": "polymarket:90090", "status": "resolved",
                  "volume_usd": 5_000_000.0}
    live_k = {**_KALSHI_ROW, "id": "kalshi:KX-LIVE", "status": "active",
              "volume_usd": 1000.0}
    live_pm = {**_PM_ROW, "id": "polymarket:90091", "status": "active",
               "volume_usd": 1000.0}
    dao = _SimpleDao({
        "kalshi:KX-OLD": settled_k, "polymarket:90090": settled_pm,
        "kalshi:KX-LIVE": live_k, "polymarket:90091": live_pm,
    })
    settled_pair = {
        "kalshi_ref": "kalshi:KX-OLD", "kalshi_ticker": "KX-OLD",
        "pm_ref": "polymarket:90090", "pm_gamma_id": "90090",
        "pm_slug": "old", "bet_type": "moneyline", "slice": "structured",
        "method": "structured_key", "confidence": 1.0,
        "kalshi_title": "old", "pm_title": "old",
    }
    live_pair = {
        "kalshi_ref": "kalshi:KX-LIVE", "kalshi_ticker": "KX-LIVE",
        "pm_ref": "polymarket:90091", "pm_gamma_id": "90091",
        "pm_slug": "live", "bet_type": "moneyline", "slice": "structured",
        "method": "structured_key", "confidence": 1.0,
        "kalshi_title": "live", "pm_title": "live",
    }
    idx = _make_index(pairs=[settled_pair, live_pair])
    status, body = await handle_markets_matched(
        {"sort_by": "volume"}, dao=dao, equivalence=idx
    )
    assert status == 200
    assert len(body["pairs"]) == 2
    # live pair first despite 5000x less volume
    assert body["pairs"][0]["kalshi"]["id"] == "kalshi:KX-LIVE"
    assert body["pairs"][0]["is_live"] is True
    assert body["pairs"][1]["is_live"] is False
