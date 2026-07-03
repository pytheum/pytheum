"""Unit suite for the pytheum SDK's typed response models (src/pytheum/client/models.py).

Every fixture below is a trimmed REAL payload sampled from
``https://api.pytheum.com`` on 2026-07-03 (see the design spec and the
module docstring in models.py for which endpoints were live-sampled vs.
inferred from server source). Each test feeds a model's ``from_dict`` a
real dict and asserts: typed fields populate correctly, ``.raw`` preserves
the original payload, an injected unknown key is silently ignored, and a
missing key tolerates (defaults to None) without raising.
"""
from __future__ import annotations

from datetime import datetime

from pytheum.client.models import (
    BookLevel,
    BookTop,
    CrossVenue,
    Divergence,
    Equivalent,
    EquivalentsResult,
    Holder,
    HoldersPage,
    LeaderboardEntry,
    LeaderboardResult,
    Market,
    MarketLeg,
    MatchedPage,
    MatchedPair,
    OHLCVBar,
    OHLCVSeries,
    Orderbook,
    PlatformStat,
    RelatedMarket,
    RelatedResult,
    ScreenPage,
    SearchPage,
    Status,
    Trade,
    Trader,
    TraderPosition,
    TradesPage,
    WhaleTrade,
    WhaleTradesPage,
)

# ---------------------------------------------------------------------------
# real fixtures (trimmed) — GET /v1/status
# ---------------------------------------------------------------------------

STATUS = {
    "equivalence": {"pairs_loaded": 142941, "dataset_version": "2026-07-01T17:07:25Z"},
    "related": {"pairs_loaded": 1488},
    "hl_related": {"pairs_loaded": 112, "dataset_version": "2026-07-03T02:51:51Z"},
    "service": {"version": "0.0.1", "now": "2026-07-03T16:50:41Z"},
    "platforms": {
        "polymarket": {"markets": 269460, "last_updated": "2026-07-03T16:43:15.845084+00:00", "status": "ok"},
        "kalshi": {"markets": 216647, "last_updated": "2026-07-03T16:33:22.362722+00:00", "status": "ok"},
        "manifold": {"markets": 19245, "last_updated": "2026-07-03T16:25:52.052536+00:00", "status": "ok"},
    },
}


def test_status_from_dict_real_sample():
    s = Status.from_dict(STATUS)
    assert s.equivalence_pairs_loaded == 142941
    assert s.equivalence_dataset_version == "2026-07-01T17:07:25Z"
    assert s.related_pairs_loaded == 1488
    assert s.hl_related_pairs_loaded == 112
    assert s.service_version == "0.0.1"
    assert s.now == datetime.fromisoformat("2026-07-03T16:50:41+00:00")
    assert set(s.platforms) == {"polymarket", "kalshi", "manifold"}
    assert isinstance(s.platforms["kalshi"], PlatformStat)
    assert s.platforms["kalshi"].markets == 216647
    assert s.platforms["kalshi"].status == "ok"
    assert s.platforms["kalshi"].last_updated == datetime.fromisoformat(
        "2026-07-03T16:33:22.362722+00:00"
    )
    assert s.raw == STATUS


def test_status_ignores_unknown_and_tolerates_missing():
    d = {**STATUS, "unexpected_future_field": {"nested": True}}
    s = Status.from_dict(d)
    assert s.equivalence_pairs_loaded == 142941  # unknown key ignored, typed fields still populate
    assert s.raw["unexpected_future_field"] == {"nested": True}  # but preserved on .raw

    s2 = Status.from_dict({})
    assert s2.equivalence_pairs_loaded is None
    assert s2.now is None
    assert s2.platforms == {}
    assert s2.raw == {}


# ---------------------------------------------------------------------------
# GET /v1/markets/matched
# ---------------------------------------------------------------------------

MATCHED_PAIR = {
    "kalshi": {
        "id": "kalshi:KXMLBGAME-26JUN291840PITPHI-PHI",
        "question": "Pittsburgh vs Philadelphia Winner?",
        "venue": "kalshi",
        "implied_yes": 0.09,
        "book": {
            "bid": 0.08, "ask": 0.09, "spread": 0.01, "last": 0.09, "day_change": -0.43,
            "bid_size": 79401.03, "ask_size": 143409.35,
        },
        "volume_usd": 2113975.69,
        "url": "https://kalshi.com/markets/KXMLBGAME-26JUN291840PITPHI-PHI",
    },
    "polymarket": {
        "id": "polymarket:2651732",
        "question": "Pittsburgh Pirates vs. Philadelphia Phillies",
        "venue": "polymarket",
        "implied_yes": None,
        "book": None,
        "volume_usd": None,
        "url": None,
    },
    "bet_type": "moneyline",
    "confidence": None,
    "method": "game_match",
    "cross_venue": {"kalshi_implied": 0.09},
    "is_live": False,
}

MATCHED_PAGE = {
    "pairs": [MATCHED_PAIR],
    "total": 142941,
    "meta": {"pairs_loaded": 142941, "filter": {"sort_by": "volume", "limit": 2, "offset": 0}},
}


def test_matched_pair_real_sample():
    p = MatchedPair.from_dict(MATCHED_PAIR)
    assert isinstance(p.kalshi, MarketLeg)
    assert p.kalshi.id == "kalshi:KXMLBGAME-26JUN291840PITPHI-PHI"
    assert p.kalshi.implied_yes == 0.09
    assert isinstance(p.kalshi.book, BookTop)
    assert p.kalshi.book.bid == 0.08
    assert p.kalshi.book.bid_size == 79401.03
    assert p.polymarket.id == "polymarket:2651732"
    assert p.polymarket.implied_yes is None
    assert p.polymarket.book is None
    assert p.bet_type == "moneyline"
    assert p.method == "game_match"
    assert isinstance(p.cross_venue, CrossVenue)
    assert p.cross_venue.kalshi_implied == 0.09
    assert p.cross_venue.net_edge is None  # never observed populated live; must not be guessed
    assert p.is_live is False
    assert p.raw == MATCHED_PAIR


def test_matched_pair_ignores_unknown_tolerates_missing():
    d = {**MATCHED_PAIR, "surprise_new_field": 42}
    p = MatchedPair.from_dict(d)
    assert p.bet_type == "moneyline"
    assert p.raw["surprise_new_field"] == 42

    p2 = MatchedPair.from_dict({})
    assert p2.bet_type is None
    assert p2.kalshi.id is None
    assert p2.cross_venue.kalshi_implied is None
    assert p2.raw == {}


def test_matched_page_real_sample():
    page = MatchedPage.from_dict(MATCHED_PAGE)
    assert page.total == 142941
    assert len(page.pairs) == 1
    assert page.pairs[0].bet_type == "moneyline"
    assert page.meta["pairs_loaded"] == 142941
    assert page.raw == MATCHED_PAGE


def test_matched_page_tolerates_missing():
    page = MatchedPage.from_dict({})
    assert page.pairs == []
    assert page.total is None
    assert page.meta is None


def test_divergence_is_matched_pair_alias():
    # find_divergences is a pure convenience wrapper over matched_pairs(sort_by="net_edge") —
    # same wire shape, so Divergence reuses MatchedPair's parser rather than duplicating it.
    assert Divergence is MatchedPair
    d = Divergence.from_dict(MATCHED_PAIR)
    assert d.bet_type == "moneyline"


# ---------------------------------------------------------------------------
# GET /v1/markets/search, /v1/markets/screen
# ---------------------------------------------------------------------------

SEARCH_MARKET = {
    "id": "polymarket:16167",
    "question": "MicroStrategy sells any Bitcoin by ___ ?",
    "venue": "polymarket",
    "bundle_id": "polymarket:economy",
    "bundle_label": "Economy",
    "status": "active",
    "volume_usd": 400473540.7159582,
    "liquidity_usd": 7778718.80864,
    "url": "https://polymarket.com/event/microstrategy-sell-any-bitcoin-in-2025",
    "resolution_at": "2027-01-01T05:00:00+00:00",
    "days_to_resolution": 181.51,
    "is_stale": False,
    "implied_yes": None,
    "book": None,
    "resolution": "This market will resolve to \"Yes\" if MicroStrategy sells any of its Bitcoin...",
    "resolution_status": None,
    "condition_id": None,
    "event_key": "polymarket-event:microstrategy-sell-any-bitcoin-in-2025",
}

SEARCH_PAGE = {
    "markets": [SEARCH_MARKET],
    "count": 1,
    "meta": {"query": "bitcoin", "tokens": ["bitcoin"], "filters": {"status": "active"}, "limit": 2},
}

SCREEN_MARKET = {
    "id": "polymarket:558936",
    "question": "Will France win the 2026 FIFA World Cup?",
    "venue": "polymarket",
    "bundle_id": "polymarket:soccer",
    "bundle_label": "Soccer",
    "status": "active",
    "volume_usd": 90462733.82588497,
    "liquidity_usd": 7751794.73743,
    "url": "https://polymarket.com/event/world-cup-winner",
    "resolution_at": "2026-07-20T00:00:00+00:00",
    "days_to_resolution": 16.3,
    "is_stale": False,
    "implied_yes": 0.3335,
    "book": {"bid": 0.333, "ask": 0.334, "spread": 0.001, "last": 0.334, "day_change": -0.014},
    "resolution": "This market will resolve according to the national team that wins...",
    "resolution_status": None,
    "condition_id": "0x9b6fef249040fd17e9c107955b37ac2c3e923509b6b0ff01cc463a331ddeb894",
    "event_key": "polymarket-event:world-cup-winner",
    "bundle_top_outcome": {"market_id": "polymarket:558936", "outcome": "France", "implied_yes": 0.3335},
    "bundle_outcomes": [{"outcome": "France", "market_id": "polymarket:558936", "implied_yes": 0.3335}],
}

SCREEN_PAGE = {
    "markets": [SCREEN_MARKET],
    "count": 1,
    "meta": {"filters": {"status": "active", "sort_by": "volume"}, "limit": 2, "dropped_stale": 0},
}


def test_market_real_sample_search():
    m = Market.from_dict(SEARCH_MARKET)
    assert m.id == "polymarket:16167"
    assert m.venue == "polymarket"
    assert m.bundle_id == "polymarket:economy"
    assert m.volume_usd == 400473540.7159582
    assert m.resolution_at == datetime.fromisoformat("2027-01-01T05:00:00+00:00")
    assert m.days_to_resolution == 181.51
    assert m.is_stale is False
    assert m.book is None
    assert m.resolution.startswith("This market will resolve to")
    assert m.event_key == "polymarket-event:microstrategy-sell-any-bitcoin-in-2025"
    assert m.raw == SEARCH_MARKET


def test_market_real_sample_screen_with_book_and_bundle_fields():
    m = Market.from_dict(SCREEN_MARKET)
    assert m.implied_yes == 0.3335
    assert isinstance(m.book, BookTop)
    assert m.book.bid == 0.333
    assert m.book.ask == 0.334
    assert m.condition_id == "0x9b6fef249040fd17e9c107955b37ac2c3e923509b6b0ff01cc463a331ddeb894"
    assert m.bundle_top_outcome["outcome"] == "France"
    assert m.bundle_outcomes[0]["outcome"] == "France"
    assert m.raw == SCREEN_MARKET


def test_market_resolution_criteria_fallback():
    # /context returns "resolution_criteria" instead of "resolution" for the same text.
    m = Market.from_dict({"id": "x", "resolution_criteria": "criteria text, no 'resolution' key"})
    assert m.resolution == "criteria text, no 'resolution' key"


def test_market_ignores_unknown_tolerates_missing():
    d = {**SEARCH_MARKET, "brand_new_field": "future"}
    m = Market.from_dict(d)
    assert m.id == "polymarket:16167"
    assert m.raw["brand_new_field"] == "future"

    m2 = Market.from_dict({})
    assert m2.id is None
    assert m2.resolution_at is None
    assert m2.book is None


def test_search_page_real_sample():
    page = SearchPage.from_dict(SEARCH_PAGE)
    assert page.count == 1
    assert page.markets[0].id == "polymarket:16167"
    assert page.meta["query"] == "bitcoin"
    assert page.raw == SEARCH_PAGE


def test_screen_page_real_sample():
    page = ScreenPage.from_dict(SCREEN_PAGE)
    assert page.count == 1
    assert page.markets[0].bundle_top_outcome["outcome"] == "France"
    assert page.meta["dropped_stale"] == 0
    assert page.raw == SCREEN_PAGE


def test_screen_page_tolerates_missing():
    page = ScreenPage.from_dict({})
    assert page.markets == []
    assert page.count is None


# ---------------------------------------------------------------------------
# GET /v1/markets/{ref}/equivalents
# ---------------------------------------------------------------------------

EQUIVALENTS_RESULT = {
    "market": {
        "id": "kalshi:KXATPGTOTAL-26JUN29ARNHAL-39",
        "question": "Matteo Arnaldi vs Quentin Halys: Total Games",
        "venue": "kalshi",
    },
    "equivalents": [
        {
            "id": "polymarket:2702984",
            "venue": "polymarket",
            "question": "Arnaldi vs. Halys: Match O/U 38.5",
            "bet_type": "tennis_total",
            "poly_side": None,
            "confidence": None,
            "method": "tennis_total_match",
            "implied_yes": None,
            "book": None,
            "volume_usd": None,
            "url": None,
        }
    ],
    "cross_venue": {},
    "meta": {"pairs_loaded": 142941, "dataset_version": "2026-07-01T17:07:25Z", "matched_via": "kalshi_ticker"},
}


def test_equivalents_result_real_sample():
    r = EquivalentsResult.from_dict(EQUIVALENTS_RESULT)
    assert r.market.id == "kalshi:KXATPGTOTAL-26JUN29ARNHAL-39"
    assert r.market.venue == "kalshi"
    assert len(r.equivalents) == 1
    eq = r.equivalents[0]
    assert isinstance(eq, Equivalent)
    assert eq.id == "polymarket:2702984"
    assert eq.bet_type == "tennis_total"
    assert eq.method == "tennis_total_match"
    assert isinstance(r.cross_venue, CrossVenue)
    assert r.cross_venue.kalshi_implied is None
    assert r.meta["matched_via"] == "kalshi_ticker"
    assert r.raw == EQUIVALENTS_RESULT


def test_equivalents_result_ignores_unknown_tolerates_missing():
    d = {**EQUIVALENTS_RESULT, "new_field": [1, 2, 3]}
    r = EquivalentsResult.from_dict(d)
    assert r.market.id == "kalshi:KXATPGTOTAL-26JUN29ARNHAL-39"
    assert r.raw["new_field"] == [1, 2, 3]

    r2 = EquivalentsResult.from_dict({})
    assert r2.equivalents == []
    assert r2.market.id is None
    assert r2.cross_venue is None


# ---------------------------------------------------------------------------
# GET /v1/markets/{ref}/related — envelope live-verified (empty related[] at
# sample time); row shape below is inferred from
# src/pytheum/api/markets_related.py:_build_related_item (see models.py note).
# ---------------------------------------------------------------------------

RELATED_RESULT_LIVE_EMPTY = {
    "market": {"id": "kalshi:KXATPGTOTAL-26JUN29ARNHAL-39", "question": None, "venue": "kalshi"},
    "related": [],
    "meta": {"pairs_loaded": 1488, "matched_via": "none", "dataset_version": "2026-07-02T05:11:43Z"},
}

RELATED_MARKET_INFERRED = {
    "id": "polymarket:558936",
    "venue": "polymarket",
    "question": "Will France win the 2026 FIFA World Cup?",
    "relation": "same_asset_different_deadline",
    "asset": "FIFA_WORLD_CUP",
    "date": "2026-07-19",
    "kalshi_band": None,
    "pm_band": None,
    "basis_note": "Kalshi settles same-day; Polymarket allows a later deadline.",
    "implied_yes": 0.3335,
    "book": {"bid": 0.333, "ask": 0.334, "spread": 0.001, "last": 0.334, "day_change": -0.014},
    "volume_usd": 90462733.82,
    "url": "https://polymarket.com/event/world-cup-winner",
}


def test_related_result_real_empty_sample():
    r = RelatedResult.from_dict(RELATED_RESULT_LIVE_EMPTY)
    assert r.market.id == "kalshi:KXATPGTOTAL-26JUN29ARNHAL-39"
    assert r.market.question is None
    assert r.related == []
    assert r.meta["matched_via"] == "none"
    assert r.raw == RELATED_RESULT_LIVE_EMPTY


def test_related_market_inferred_row_shape():
    rm = RelatedMarket.from_dict(RELATED_MARKET_INFERRED)
    assert rm.id == "polymarket:558936"
    assert rm.relation == "same_asset_different_deadline"
    assert rm.basis_note.startswith("Kalshi settles")
    assert isinstance(rm.book, BookTop)
    assert rm.book.bid == 0.333
    assert rm.raw == RELATED_MARKET_INFERRED


def test_related_market_ignores_unknown_tolerates_missing():
    d = {**RELATED_MARKET_INFERRED, "extra": True}
    rm = RelatedMarket.from_dict(d)
    assert rm.id == "polymarket:558936"
    assert rm.raw["extra"] is True

    rm2 = RelatedMarket.from_dict({})
    assert rm2.id is None
    assert rm2.book is None


# ---------------------------------------------------------------------------
# GET /v1/markets/{ref}/book
# ---------------------------------------------------------------------------

ORDERBOOK = {
    "bids": [[0.333, 190201.11], [0.332, 12317.22], [0.33, 7652481.31]],
    "asks": [[0.334, 227541.25], [0.335, 4854.5]],
    "venue": "polymarket",
    "ref": "polymarket:558936",
    "ts": "2026-07-03T16:51:07.114874+00:00",
    "source": "live",
    "top": {
        "bid": 0.333, "bid_size": 190201.11, "ask": 0.334, "ask_size": 227541.25,
        "spread": 0.001, "mid": 0.3335, "mid_reliable": True,
    },
}


def test_orderbook_real_sample():
    ob = Orderbook.from_dict(ORDERBOOK)
    assert ob.venue == "polymarket"
    assert ob.ref == "polymarket:558936"
    assert ob.source == "live"
    assert ob.ts == datetime.fromisoformat("2026-07-03T16:51:07.114874+00:00")
    assert len(ob.bids) == 3
    assert isinstance(ob.bids[0], BookLevel)
    assert ob.bids[0].price == 0.333
    assert ob.bids[0].size == 190201.11
    assert ob.asks[1].price == 0.335
    assert isinstance(ob.top, BookTop)
    assert ob.top.mid == 0.3335
    assert ob.top.mid_reliable is True
    assert ob.raw == ORDERBOOK


def test_orderbook_ignores_unknown_tolerates_missing():
    d = {**ORDERBOOK, "unknown_key": "z"}
    ob = Orderbook.from_dict(d)
    assert ob.venue == "polymarket"
    assert ob.raw["unknown_key"] == "z"

    ob2 = Orderbook.from_dict({})
    assert ob2.bids == []
    assert ob2.top is None


def test_book_level_accepts_wire_list_form():
    lvl = BookLevel.from_dict([0.333, 190201.11])
    assert lvl.price == 0.333
    assert lvl.size == 190201.11
    assert lvl.raw == {"price": 0.333, "size": 190201.11}


def test_book_level_tolerates_short_list():
    lvl = BookLevel.from_dict([0.333])
    assert lvl.price == 0.333
    assert lvl.size is None


# ---------------------------------------------------------------------------
# GET /v1/markets/{ref}/trades
# ---------------------------------------------------------------------------

TRADES_PAGE = {
    "trades": [
        {"ts": "2026-07-03T16:45:52+00:00", "price": 0.334, "size": 71.47, "side": "BUY"},
        {"ts": "2026-07-03T16:45:25+00:00", "price": 0.667, "size": 1.4992, "side": "BUY"},
    ],
    "venue": "polymarket",
    "ref": "polymarket:558936",
    "count": 100,
    "source": "live",
    "newest_trade_age_s": 442.2,
    "is_stale": False,
}


def test_trades_page_real_sample():
    page = TradesPage.from_dict(TRADES_PAGE)
    assert page.count == 100
    assert page.source == "live"
    assert page.is_stale is False
    assert len(page.trades) == 2
    t = page.trades[0]
    assert isinstance(t, Trade)
    assert t.ts == datetime.fromisoformat("2026-07-03T16:45:52+00:00")
    assert t.price == 0.334
    assert t.side == "BUY"
    assert page.raw == TRADES_PAGE


def test_trades_page_ignores_unknown_tolerates_missing():
    d = {**TRADES_PAGE, "future_field": 1}
    page = TradesPage.from_dict(d)
    assert page.count == 100
    assert page.raw["future_field"] == 1

    page2 = TradesPage.from_dict({})
    assert page2.trades == []
    assert page2.count is None


# ---------------------------------------------------------------------------
# GET /v1/markets/{ref}/ohlcv
# ---------------------------------------------------------------------------

OHLCV_SERIES = {
    "market": {"id": "polymarket:558936", "question": "Will France win the 2026 FIFA World Cup?", "venue": "polymarket"},
    "interval": "1h",
    "candles": [
        {"t": "2026-07-02T22:00:00Z", "o": 0.32, "h": 0.357, "l": 0.32, "c": 0.357, "v": 419},
        {"t": "2026-07-02T23:00:00Z", "o": 0.356, "h": 0.997, "l": 0.003, "c": 0.31, "v": 13957},
    ],
    "meta": {"source": "pit_archive", "count": 2, "partial_last_bucket": False},
}


def test_ohlcv_series_real_sample():
    series = OHLCVSeries.from_dict(OHLCV_SERIES)
    assert series.interval == "1h"
    assert series.market.id == "polymarket:558936"
    assert len(series.candles) == 2
    bar = series.candles[0]
    assert isinstance(bar, OHLCVBar)
    # "t" ends in Z on the wire -> parsed as UTC.
    assert bar.t == datetime.fromisoformat("2026-07-02T22:00:00+00:00")
    assert bar.o == 0.32
    assert bar.h == 0.357
    assert bar.l == 0.32
    assert bar.c == 0.357
    assert bar.v == 419
    assert series.meta["source"] == "pit_archive"
    assert series.raw == OHLCV_SERIES


def test_ohlcv_series_ignores_unknown_tolerates_missing():
    d = {**OHLCV_SERIES, "surprise": "x"}
    series = OHLCVSeries.from_dict(d)
    assert series.interval == "1h"
    assert series.raw["surprise"] == "x"

    series2 = OHLCVSeries.from_dict({})
    assert series2.candles == []
    assert series2.market is None


# ---------------------------------------------------------------------------
# GET /v1/traders/leaderboard, GET /v1/traders/{wallet}
# ---------------------------------------------------------------------------

LEADERBOARD_RESULT = {
    "period": "weekly",
    "traders": [
        {"name": None, "address": "0x7c1ee865a785de4c00ee90ed86a38489fb8bbab3",
         "profit": 523489.5101235178, "volume": None, "positions_value": None, "rank": "1"},
        {"name": None, "address": "0x10a6fadcbacd66330862206f6199b197e3ad4d8b",
         "profit": 523380.2545414589, "volume": None, "positions_value": None, "rank": "2"},
    ],
}

TRADER = {
    "wallet": "0x7c1ee865a785de4c00ee90ed86a38489fb8bbab3",
    "positions": [
        {"market": "0x3a26ca6425e2d98f14935670bc22cdb0744defc6f6d83c65f8c413a921c5c70c",
         "outcome": "Yes", "size": 944879.9564, "avg_price": 0.0089, "current_value": 8031.4796, "profit": None},
        {"market": "0xeb0de200851ebb80eb722c8b02e2226fe76e91e86a7cfe7e38042632e42d5ae9",
         "outcome": "Yes", "size": 680000.0, "avg_price": 0.3, "current_value": 0.0, "profit": None},
    ],
}


def test_leaderboard_result_real_sample():
    r = LeaderboardResult.from_dict(LEADERBOARD_RESULT)
    assert r.period == "weekly"
    assert len(r.traders) == 2
    e = r.traders[0]
    assert isinstance(e, LeaderboardEntry)
    assert e.address == "0x7c1ee865a785de4c00ee90ed86a38489fb8bbab3"
    assert e.profit == 523489.5101235178
    assert e.rank == "1"  # rank is a string on the wire — kept as-is
    assert r.raw == LEADERBOARD_RESULT


def test_leaderboard_result_ignores_unknown_tolerates_missing():
    d = {**LEADERBOARD_RESULT, "new": 1}
    r = LeaderboardResult.from_dict(d)
    assert r.period == "weekly"
    assert r.raw["new"] == 1

    r2 = LeaderboardResult.from_dict({})
    assert r2.traders == []
    assert r2.period is None


def test_trader_real_sample():
    t = Trader.from_dict(TRADER)
    assert t.wallet == "0x7c1ee865a785de4c00ee90ed86a38489fb8bbab3"
    assert len(t.positions) == 2
    pos = t.positions[0]
    assert isinstance(pos, TraderPosition)
    assert pos.outcome == "Yes"
    assert pos.size == 944879.9564
    assert pos.avg_price == 0.0089
    assert pos.profit is None
    assert t.raw == TRADER


def test_trader_ignores_unknown_tolerates_missing():
    d = {**TRADER, "flag": True}
    t = Trader.from_dict(d)
    assert t.wallet == "0x7c1ee865a785de4c00ee90ed86a38489fb8bbab3"
    assert t.raw["flag"] is True

    t2 = Trader.from_dict({})
    assert t2.positions == []
    assert t2.wallet is None


# ---------------------------------------------------------------------------
# GET /v1/markets/{ref}/holders
# ---------------------------------------------------------------------------

HOLDERS_PAGE = {
    "holders": [
        {"address": "0x85f031d069de300055900c4055c1baeb6bde3f67", "amount": 2465264.95018,
         "outcome": "108233603819467706476318984012158651931658302669301887462181073562758483842092"},
        {"address": "0xae2b0aaea325a32870f56ff19df3b87acaa190ea", "amount": 2053298.976137,
         "outcome": "108233603819467706476318984012158651931658302669301887462181073562758483842092"},
    ],
}


def test_holders_page_real_sample():
    page = HoldersPage.from_dict(HOLDERS_PAGE)
    assert len(page.holders) == 2
    h = page.holders[0]
    assert isinstance(h, Holder)
    assert h.address == "0x85f031d069de300055900c4055c1baeb6bde3f67"
    assert h.amount == 2465264.95018
    assert page.raw == HOLDERS_PAGE


def test_holders_page_ignores_unknown_tolerates_missing():
    d = {**HOLDERS_PAGE, "count": 2, "future": "x"}
    page = HoldersPage.from_dict(d)
    assert page.count == 2
    assert page.raw["future"] == "x"

    page2 = HoldersPage.from_dict({})
    assert page2.holders == []


# ---------------------------------------------------------------------------
# GET /v1/markets/whale-trades
# ---------------------------------------------------------------------------

WHALE_TRADES_PAGE = {
    "trades": [
        {
            "ts": "2026-07-03T16:47:08+00:00",
            "market": "0x59130ac0b640704060a42768d973d95c046c43a7d4c93c65ddbe99648c444309",
            "price": 0.41,
            "size": 2357.08,
            "notional_usd": 966.4028,
            "side": "SELL",
            "wallet": "0x4ebc2722adc772bde8680792d0a6fdf15499a33d",
            "pseudonym": "Delicious-Attacker",
        }
    ],
    "count": 1,
    "min_usd": 500.0,
    "venue": "polymarket",
    "source": "live",
    "note": "Polymarket-only. Kalshi trades are anonymized.",
}


def test_whale_trades_page_real_sample():
    page = WhaleTradesPage.from_dict(WHALE_TRADES_PAGE)
    assert page.count == 1
    assert page.min_usd == 500.0
    assert page.note.startswith("Polymarket-only")
    assert len(page.trades) == 1
    wt = page.trades[0]
    assert isinstance(wt, WhaleTrade)
    assert wt.ts == datetime.fromisoformat("2026-07-03T16:47:08+00:00")
    assert wt.notional_usd == 966.4028
    assert wt.pseudonym == "Delicious-Attacker"
    assert wt.side == "SELL"
    assert page.raw == WHALE_TRADES_PAGE


def test_whale_trades_page_ignores_unknown_tolerates_missing():
    d = {**WHALE_TRADES_PAGE, "unexpected": [1]}
    page = WhaleTradesPage.from_dict(d)
    assert page.count == 1
    assert page.raw["unexpected"] == [1]

    page2 = WhaleTradesPage.from_dict({})
    assert page2.trades == []
    assert page2.min_usd is None


# ---------------------------------------------------------------------------
# from_dict robustness: non-dict input never raises
# ---------------------------------------------------------------------------

def test_from_dict_handles_none_and_non_dict_gracefully():
    assert Status.from_dict(None).raw == {}
    assert MatchedPair.from_dict(None).raw == {}
    assert Market.from_dict(None).raw == {}
    assert Orderbook.from_dict(None).raw == {}
    assert TradesPage.from_dict(None).raw == {}
