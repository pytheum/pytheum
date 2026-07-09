"""Bug 1 regression — matched live_only / net_edge radar sources genuinely-live pairs
from the DB JOIN, not the volume-ordered static index head.

The prod bug: `handle_markets_matched` paged the static EquivalenceIndex (volume order)
BEFORE liveness was known, so the first page was 100% resolved-but-status='active' sports;
post-hydration liveness pruning then returned 0 for `live_only` and phantom ~1.0 legs for
the net_edge radar. Fix: when it's the unfiltered live radar, draw candidates from
`dao.fetch_live_matched_pairs` (both legs active AND unresolved).
"""
from __future__ import annotations

from pytheum.api.markets_matched import handle_markets_matched
from tests.test_markets_matched import _KALSHI_ROW, _PM_ROW, _SimpleDao, _make_index

# Genuinely-live legs (active, unresolved).
_LIVE_K = {**_KALSHI_ROW, "id": "kalshi:KX-LIVE", "status": "active", "resolution_at": None,
           "volume_usd": 1000.0}
_LIVE_PM = {**_PM_ROW, "id": "polymarket:LIVE", "status": "active", "resolution_at": None,
            "volume_usd": 1000.0}
# Resolved-but-status='active' legs with huge lifetime volume — the phantom class that the
# static index front-loads. resolution_at in the past ⇒ _leg_live must mark them dead.
_DEAD_PAYLOAD = {"outcomePrices": "[0.99, 0.01]", "bestBid": "0.98", "bestAsk": "1.0"}
_DEAD_K = {**_KALSHI_ROW, "id": "kalshi:KX-DEAD", "status": "active",
           "resolution_at": "2020-01-01T00:00:00Z", "volume_usd": 5_000_000.0,
           "payload": _DEAD_PAYLOAD}
_DEAD_PM = {**_PM_ROW, "id": "polymarket:DEAD", "status": "active",
            "resolution_at": "2020-01-01T00:00:00Z", "volume_usd": 5_000_000.0,
            "payload": _DEAD_PAYLOAD}

# What the volume-ordered index front-loads (the dead pair).
_DEAD_PAIR_IDX = {
    "kalshi_ref": "kalshi:KX-DEAD", "kalshi_ticker": "KX-DEAD",
    "pm_ref": "polymarket:DEAD", "pm_gamma_id": "DEAD", "pm_slug": "dead",
    "bet_type": "moneyline", "method": "structured_key", "confidence": 1.0,
    "kalshi_title": "dead", "pm_title": "dead",
}


class _LiveDao(_SimpleDao):
    """DAO that also implements the Bug-1 fix method fetch_live_matched_pairs."""

    def __init__(self, store, live_rows):
        super().__init__(store)
        self._live_rows = live_rows

    async def fetch_live_matched_pairs(self, *, bet_types=None, limit=500):
        rows = self._live_rows
        if bet_types:
            rows = [r for r in rows if r["bet_type"] in bet_types]
        return rows[:limit]


def _store():
    return {r["id"]: r for r in (_LIVE_K, _LIVE_PM, _DEAD_K, _DEAD_PM)}


def _live_rows():
    # market_equivalence-shaped rows (what the JOIN returns).
    return [{
        "kalshi_market_id": "kalshi:KX-LIVE", "polymarket_market_id": "polymarket:LIVE",
        "method": "structured_key", "confidence": 1.0, "bet_type": "moneyline",
    }]


async def test_net_edge_radar_sources_live_from_db_not_dead_index():
    # Index front-loads the DEAD pair (as prod's volume order does); DAO returns the LIVE one.
    idx = _make_index(pairs=[_DEAD_PAIR_IDX])
    dao = _LiveDao(_store(), _live_rows())
    status, body = await handle_markets_matched({"sort_by": "net_edge"}, dao=dao, equivalence=idx)
    assert status == 200
    refs = [p["kalshi"]["id"] for p in body["pairs"]]
    assert "kalshi:KX-LIVE" in refs           # live pair surfaced from the DB JOIN
    assert "kalshi:KX-DEAD" not in refs        # the dead index head was bypassed
    assert all(p["is_live"] for p in body["pairs"])


async def test_live_only_returns_live_not_zero():
    idx = _make_index(pairs=[_DEAD_PAIR_IDX])
    dao = _LiveDao(_store(), _live_rows())
    status, body = await handle_markets_matched({"live_only": "true"}, dao=dao, equivalence=idx)
    assert status == 200
    assert len(body["pairs"]) == 1            # NOT zero (the bug)
    assert body["pairs"][0]["kalshi"]["id"] == "kalshi:KX-LIVE"


async def test_bet_type_filter_passes_through_to_db():
    idx = _make_index(pairs=[_DEAD_PAIR_IDX])
    dao = _LiveDao(_store(), _live_rows())
    # moneyline matches → returned; a non-matching bet_type → empty.
    _, body_ml = await handle_markets_matched(
        {"sort_by": "net_edge", "bet_type": "moneyline"}, dao=dao, equivalence=idx)
    assert [p["kalshi"]["id"] for p in body_ml["pairs"]] == ["kalshi:KX-LIVE"]
    _, body_total = await handle_markets_matched(
        {"sort_by": "net_edge", "bet_type": "total"}, dao=dao, equivalence=idx)
    assert body_total["pairs"] == []


async def test_fallback_to_index_when_dao_lacks_method():
    # A DAO without fetch_live_matched_pairs → index path (backward compatible).
    live_idx_pair = {**_DEAD_PAIR_IDX, "kalshi_ref": "kalshi:KX-LIVE", "kalshi_ticker": "KX-LIVE",
                     "pm_ref": "polymarket:LIVE", "pm_gamma_id": "LIVE", "pm_slug": "live"}
    idx = _make_index(pairs=[live_idx_pair])
    dao = _SimpleDao(_store())
    status, body = await handle_markets_matched({"sort_by": "net_edge"}, dao=dao, equivalence=idx)
    assert status == 200
    assert "kalshi:KX-LIVE" in [p["kalshi"]["id"] for p in body["pairs"]]


async def test_league_filter_keeps_index_path_not_db():
    # A league/date/q filter must keep the index path (DB gate excludes filtered browsing).
    called = {"db": False}

    class _TrackingDao(_LiveDao):
        async def fetch_live_matched_pairs(self, *, bet_types=None, limit=500):
            called["db"] = True
            return await super().fetch_live_matched_pairs(bet_types=bet_types, limit=limit)

    idx = _make_index(pairs=[_DEAD_PAIR_IDX])
    dao = _TrackingDao(_store(), _live_rows())
    await handle_markets_matched({"sort_by": "net_edge", "league": "nba"}, dao=dao, equivalence=idx)
    assert called["db"] is False   # league filter → index path; DB source not used
