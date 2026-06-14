"""Tests for league= and date= filters on /matched and browse().

Three layers:
  1. EquivalenceIndex.browse() unit tests (in-memory filter logic)
  2. handle_markets_matched handler tests (filter parsing + meta echo)
"""
from __future__ import annotations

import pytest

from pytheum.api.markets_matched import handle_markets_matched
from pytheum.equivalence.index import EquivalenceIndex

# ---------------------------------------------------------------------------
# Fixture data
# ---------------------------------------------------------------------------

_NBA_GAME = {
    "kalshi_ticker": "KX-NBA-LAL-BOS",
    "kalshi_ref": "kalshi:KX-NBA-LAL-BOS",
    "pm_ref": "polymarket:10001",
    "pm_gamma_id": "10001",
    "pm_slug": "lakers-celtics",
    "bet_type": "moneyline",
    "league": "NBA",
    "game_date": "2026-06-15",
    "kalshi_title": "Will the Lakers beat the Celtics?",
    "pm_title": "Lakers vs Celtics winner",
    "confidence": 1.0,
    "method": "structured_key",
}

_NFL_GAME = {
    "kalshi_ticker": "KX-NFL-KC-LV",
    "kalshi_ref": "kalshi:KX-NFL-KC-LV",
    "pm_ref": "polymarket:10002",
    "pm_gamma_id": "10002",
    "pm_slug": "chiefs-raiders",
    "bet_type": "moneyline",
    "league": "NFL",
    "game_date": "2026-06-20",
    "kalshi_title": "Will the Chiefs beat the Raiders?",
    "pm_title": "Chiefs vs Raiders winner",
    "confidence": 1.0,
    "method": "structured_key",
}

_NBA_GAME_2 = {
    "kalshi_ticker": "KX-NBA-GSW-DEN",
    "kalshi_ref": "kalshi:KX-NBA-GSW-DEN",
    "pm_ref": "polymarket:10003",
    "pm_gamma_id": "10003",
    "pm_slug": "warriors-nuggets",
    "bet_type": "moneyline",
    "league": "NBA",
    "game_date": "2026-06-15",
    "kalshi_title": "Will the Warriors beat the Nuggets?",
    "pm_title": "Warriors vs Nuggets winner",
    "confidence": 1.0,
    "method": "structured_key",
}

_NO_LEAGUE = {
    "kalshi_ticker": "KX-ELECTION-2026",
    "kalshi_ref": "kalshi:KX-ELECTION-2026",
    "pm_ref": "polymarket:10004",
    "pm_gamma_id": "10004",
    "pm_slug": "us-election-2026",
    "bet_type": "event",
    "kalshi_title": "Will Democrats win the 2026 Senate?",
    "pm_title": "Democrats win 2026 Senate?",
    "confidence": 1.0,
    "method": "blocked_deterministic",
}

_NBA_NO_DATE = {
    "kalshi_ticker": "KX-NBA-NODATEPAIR",
    "kalshi_ref": "kalshi:KX-NBA-NODATEPAIR",
    "pm_ref": "polymarket:10005",
    "pm_gamma_id": "10005",
    "pm_slug": "no-date-pair",
    "bet_type": "moneyline",
    "league": "NBA",
    # No game_date field
    "kalshi_title": "NBA no-date pair",
    "pm_title": "NBA no-date pm",
    "confidence": 1.0,
    "method": "structured_key",
}

_ALL_PAIRS = [_NBA_GAME, _NFL_GAME, _NBA_GAME_2, _NO_LEAGUE, _NBA_NO_DATE]


def _make_index(pairs=None) -> EquivalenceIndex:
    idx = EquivalenceIndex()
    idx.dataset_version = "2026-06-12T00:00:00Z"
    for row in (pairs if pairs is not None else _ALL_PAIRS):
        idx._rows.append(row)
        kt = row.get("kalshi_ticker")
        if kt:
            idx._by_kalshi_ticker.setdefault(kt, []).append(row)
        gid = row.get("pm_gamma_id")
        if gid:
            idx._by_pm_gamma_id.setdefault(str(gid), []).append(row)
    return idx


class _SimpleDao:
    async def fetch_markets_by_ids(self, ids):
        return []


# ---------------------------------------------------------------------------
# EquivalenceIndex.browse() — league filter
# ---------------------------------------------------------------------------


def test_browse_league_filter_exact():
    idx = _make_index()
    rows, total = idx.browse(league="NBA")
    assert total == 3  # NBA_GAME, NBA_GAME_2, NBA_NO_DATE
    assert all(r.get("league") == "NBA" for r in rows)


def test_browse_league_filter_case_insensitive():
    idx = _make_index()
    rows, total = idx.browse(league="nba")
    assert total == 3


def test_browse_league_filter_excludes_no_league_rows():
    """Rows without a league field must be excluded when league filter is active."""
    idx = _make_index()
    rows, total = idx.browse(league="NBA")
    ids = [r["kalshi_ticker"] for r in rows]
    assert "KX-ELECTION-2026" not in ids
    assert "KX-NFL-KC-LV" not in ids


def test_browse_league_filter_no_match():
    idx = _make_index()
    rows, total = idx.browse(league="NHL")
    assert total == 0
    assert rows == []


def test_browse_league_filter_nfl():
    idx = _make_index()
    rows, total = idx.browse(league="NFL")
    assert total == 1
    assert rows[0]["kalshi_ticker"] == "KX-NFL-KC-LV"


# ---------------------------------------------------------------------------
# EquivalenceIndex.browse() — date filter
# ---------------------------------------------------------------------------


def test_browse_date_filter():
    idx = _make_index()
    rows, total = idx.browse(game_date="2026-06-15")
    assert total == 2
    tickers = {r["kalshi_ticker"] for r in rows}
    assert "KX-NBA-LAL-BOS" in tickers
    assert "KX-NBA-GSW-DEN" in tickers


def test_browse_date_filter_excludes_no_date_rows():
    idx = _make_index()
    rows, total = idx.browse(game_date="2026-06-15")
    ids = [r["kalshi_ticker"] for r in rows]
    assert "KX-NBA-NODATEPAIR" not in ids


def test_browse_date_filter_no_match():
    idx = _make_index()
    rows, total = idx.browse(game_date="2025-01-01")
    assert total == 0


def test_browse_date_filter_different_date():
    idx = _make_index()
    rows, total = idx.browse(game_date="2026-06-20")
    assert total == 1
    assert rows[0]["kalshi_ticker"] == "KX-NFL-KC-LV"


# ---------------------------------------------------------------------------
# EquivalenceIndex.browse() — combined league + date filter
# ---------------------------------------------------------------------------


def test_browse_league_and_date_combined():
    """league=NBA + date=2026-06-15 → only games that match BOTH."""
    idx = _make_index()
    rows, total = idx.browse(league="NBA", game_date="2026-06-15")
    assert total == 2
    tickers = {r["kalshi_ticker"] for r in rows}
    assert "KX-NBA-LAL-BOS" in tickers
    assert "KX-NBA-GSW-DEN" in tickers
    assert "KX-NFL-KC-LV" not in tickers


def test_browse_league_and_date_no_match():
    idx = _make_index()
    rows, total = idx.browse(league="NFL", game_date="2026-06-15")
    assert total == 0


# ---------------------------------------------------------------------------
# EquivalenceIndex.leagues_available()
# ---------------------------------------------------------------------------


def test_leagues_available():
    idx = _make_index()
    leagues = idx.leagues_available()
    assert "NBA" in leagues
    assert "NFL" in leagues
    assert len(leagues) == 2
    assert leagues == sorted(leagues)


def test_leagues_available_empty_index():
    idx = _make_index(pairs=[])
    assert idx.leagues_available() == []


def test_leagues_available_max_values():
    idx = EquivalenceIndex()
    for i in range(60):
        idx._rows.append({"league": f"LEAGUE_{i:02d}", "kalshi_ticker": f"KX-{i}"})
    leagues = idx.leagues_available(max_values=50)
    assert len(leagues) <= 50


# ---------------------------------------------------------------------------
# handle_markets_matched handler — filter echo + leagues_available in meta
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_handler_league_filter():
    dao = _SimpleDao()
    idx = _make_index()
    _, body = await handle_markets_matched({"league": "NBA"}, dao=dao, equivalence=idx)
    assert body["total"] == 3
    assert body["meta"]["filter"]["league"] == "NBA"


@pytest.mark.asyncio
async def test_handler_date_filter():
    dao = _SimpleDao()
    idx = _make_index()
    _, body = await handle_markets_matched({"date": "2026-06-15"}, dao=dao, equivalence=idx)
    assert body["total"] == 2
    assert body["meta"]["filter"]["date"] == "2026-06-15"


@pytest.mark.asyncio
async def test_handler_game_date_alias():
    """?game_date= should work as an alias for ?date=."""
    dao = _SimpleDao()
    idx = _make_index()
    _, body = await handle_markets_matched({"game_date": "2026-06-15"}, dao=dao, equivalence=idx)
    assert body["total"] == 2


@pytest.mark.asyncio
async def test_handler_league_and_date_combined():
    dao = _SimpleDao()
    idx = _make_index()
    _, body = await handle_markets_matched(
        {"league": "NBA", "date": "2026-06-15"}, dao=dao, equivalence=idx
    )
    assert body["total"] == 2
    assert body["meta"]["filter"]["league"] == "NBA"
    assert body["meta"]["filter"]["date"] == "2026-06-15"


@pytest.mark.asyncio
async def test_handler_filter_not_in_meta_when_absent():
    """When no league/date filter is applied, those keys must not appear in filter."""
    dao = _SimpleDao()
    idx = _make_index()
    _, body = await handle_markets_matched({}, dao=dao, equivalence=idx)
    filt = body["meta"]["filter"]
    assert "league" not in filt
    assert "date" not in filt


@pytest.mark.asyncio
async def test_handler_leagues_available_in_meta():
    """leagues_available should appear in meta when the dataset has league data."""
    dao = _SimpleDao()
    idx = _make_index()
    _, body = await handle_markets_matched({}, dao=dao, equivalence=idx)
    meta = body["meta"]
    assert "leagues_available" in meta
    assert "NBA" in meta["leagues_available"]
    assert "NFL" in meta["leagues_available"]


@pytest.mark.asyncio
async def test_handler_leagues_available_absent_for_no_league_data():
    """If no rows have a league field, leagues_available must not appear."""
    dao = _SimpleDao()
    idx = _make_index(pairs=[_NO_LEAGUE])
    _, body = await handle_markets_matched({}, dao=dao, equivalence=idx)
    assert "leagues_available" not in body["meta"]


@pytest.mark.asyncio
async def test_handler_malformed_date_ignored():
    """A malformed date param should be treated as no filter (silently ignored)."""
    dao = _SimpleDao()
    idx = _make_index()
    _, body = await handle_markets_matched({"date": "not-a-date"}, dao=dao, equivalence=idx)
    assert body["total"] == 5
    assert "date" not in body["meta"]["filter"]
