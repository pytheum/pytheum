"""Coverage tests for the smaller API modules:

markets_matched residual branches (_parse_offset / _parse_min_volume guards,
net_edge sort, fungible_excluded, league/date echo, degraded), markets_rules
(deadline compare, fallbacks, degraded), status (stale guard, version, platforms,
cache, degraded singletons), markets_get (raw-ticker fallback), ref_utils (URL
extraction), gate (_prune, non-http passthrough, _client_ip XFF), and the
register_all wiring in api/__init__.
"""
from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from pytheum.api import register_all, register_group_A, register_group_B
from pytheum.api import status as status_mod
from pytheum.api.gate import ApiGate, _client_ip
from pytheum.api.markets_get import handle_market_get
from pytheum.api.markets_matched import (
    _parse_min_volume,
    _parse_offset,
    handle_markets_matched,
)
from pytheum.api.markets_rules import (
    _compare_deadlines,
    _market_rules_block,
    handle_market_rules,
)
from pytheum.api.ref_utils import normalize_ref
from pytheum.api.status import _get_version, _is_stale, handle_status
from pytheum.equivalence.index import EquivalenceIndex
from pytheum.registry import RouterRegistry

# =========================================================================== #
# markets_matched residual
# =========================================================================== #


def test_parse_offset_guards() -> None:
    assert _parse_offset({}) == 0
    assert _parse_offset({"offset": "5"}) == 5
    assert _parse_offset({"offset": "-3"}) == 0
    assert _parse_offset({"offset": "bad"}) == 0


def test_parse_min_volume_guards() -> None:
    assert _parse_min_volume({}) is None
    assert _parse_min_volume({"min_volume": "100"}) == 100.0
    assert _parse_min_volume({"min_volume": "bad"}) is None


def _matched_index(rows: list[dict[str, Any]]) -> EquivalenceIndex:
    idx = EquivalenceIndex()
    idx.dataset_version = "2026-06-12T00:00:00Z"
    for r in rows:
        idx._rows.append(r)
        kt = r.get("kalshi_ticker")
        if kt:
            idx._by_kalshi_ticker.setdefault(kt, []).append(r)
    return idx


class _BatchDao:
    def __init__(self, store: dict[str, dict[str, Any]]) -> None:
        self._store = store

    async def fetch_markets_by_ids(self, ids: list[str]) -> list[dict[str, Any]]:
        return [self._store[i] for i in ids if i in self._store]


def _pair(ticker: str, gid: str, *, method: str = "structured_key", bt: str = "moneyline",
          league: str | None = None, date: str | None = None) -> dict[str, Any]:
    p = {
        "kalshi_ref": f"kalshi:{ticker}", "kalshi_ticker": ticker,
        "pm_ref": f"polymarket:{gid}", "pm_gamma_id": gid,
        "bet_type": bt, "method": method, "confidence": 1.0,
        "kalshi_title": f"{ticker} k", "pm_title": f"{gid} p",
    }
    if league is not None:
        p["league"] = league
    if date is not None:
        p["game_date"] = date
    return p


async def test_matched_net_edge_sort() -> None:
    idx = _matched_index([_pair("A", "1"), _pair("B", "2")])
    # both legs booked → locked_arb_net_edge computable; A wider book.
    store = {
        "kalshi:A": {"id": "kalshi:A", "venue": "kalshi", "status": "active",
                     "volume_usd": 1.0,
                     "payload": {"outcomePrices": "[0.5,0.5]", "bestBid": "0.10", "bestAsk": "0.20"}},
        "polymarket:1": {"id": "polymarket:1", "venue": "polymarket", "status": "active",
                         "volume_usd": 1.0,
                         "payload": {"outcomePrices": "[0.5,0.5]", "bestBid": "0.85", "bestAsk": "0.95"}},
        "kalshi:B": {"id": "kalshi:B", "venue": "kalshi", "status": "active",
                     "volume_usd": 1.0,
                     "payload": {"outcomePrices": "[0.5,0.5]", "bestBid": "0.49", "bestAsk": "0.51"}},
        "polymarket:2": {"id": "polymarket:2", "venue": "polymarket", "status": "active",
                         "volume_usd": 1.0,
                         "payload": {"outcomePrices": "[0.5,0.5]", "bestBid": "0.49", "bestAsk": "0.51"}},
    }
    _, body = await handle_markets_matched(
        {"sort_by": "net_edge"}, dao=_BatchDao(store), equivalence=idx
    )
    # net_edge IS a valid sort (fixed 2026-07-03 — the allowlist had lagged the
    # implemented _build_sort_key branch, silently serving volume order for the
    # advertised honest-arb-radar sort).
    assert body["meta"]["filter"]["sort_by"] == "net_edge"
    assert "net_edge" in body["pairs"][0]["cross_venue"]
    assert "executable" in body["pairs"][0]["cross_venue"]


async def test_matched_min_volume_filter_and_overfetch() -> None:
    idx = _matched_index([_pair("A", "1"), _pair("B", "2")])
    store = {
        "kalshi:A": {"id": "kalshi:A", "venue": "kalshi", "status": "active",
                     "volume_usd": 50.0, "payload": {}},
        "kalshi:B": {"id": "kalshi:B", "venue": "kalshi", "status": "active",
                     "volume_usd": 5000.0, "payload": {}},
    }
    _, body = await handle_markets_matched(
        {"min_volume": "1000"}, dao=_BatchDao(store), equivalence=idx
    )
    ids = [p["kalshi"]["id"] for p in body["pairs"]]
    assert "kalshi:B" in ids
    assert "kalshi:A" not in ids  # below threshold


async def test_matched_fungible_excluded_and_filters_echo() -> None:
    idx = _matched_index([
        _pair("A", "1", method="structured_key", league="NBA", date="2026-07-01"),
        _pair("B", "2", method="opus_backstop", league="NBA"),
    ])
    _, body = await handle_markets_matched(
        {"fungible_only": "true", "league": "NBA", "date": "2026-07-01"},
        dao=_BatchDao({}), equivalence=idx,
    )
    assert body["meta"]["filter"]["fungible_only"] is True
    assert body["meta"]["filter"]["league"] == "NBA"
    assert body["meta"]["filter"]["date"] == "2026-07-01"
    assert "fungible_excluded" in body["meta"]
    assert "leagues_available" in body["meta"]


async def test_matched_degraded_file_missing() -> None:
    idx = EquivalenceIndex()
    idx.file_missing = True
    _, body = await handle_markets_matched({}, dao=_BatchDao({}), equivalence=idx)
    assert body["meta"]["degraded"] is True
    assert body["meta"]["degraded_reason"] == "equivalence_file_not_found"


async def test_matched_dao_exception_swallowed() -> None:
    idx = _matched_index([_pair("A", "1")])

    class _BoomDao:
        async def fetch_markets_by_ids(self, ids: list[str]) -> list[dict[str, Any]]:
            raise RuntimeError("db down")

    _, body = await handle_markets_matched({}, dao=_BoomDao(), equivalence=idx)
    # null hydration but still 200 with the pair.
    assert body["total"] == 1
    assert body["pairs"][0]["kalshi"]["implied_yes"] is None


# =========================================================================== #
# markets_rules
# =========================================================================== #



def test_rules_block_absent_and_present() -> None:
    absent = _market_rules_block(None, ref="kalshi:X", question="Q", venue="kalshi")
    assert absent["resolution"] is None and absent["question"] == "Q"
    dt = datetime(2026, 7, 1, tzinfo=UTC)
    present = _market_rules_block(
        {"id": "kalshi:X", "venue": "kalshi", "question": "Real?",
         "resolution_at": dt, "url": "u",
         "payload": {"rulesPrimary": "Resolves YES if..."}},
        ref="kalshi:X",
    )
    assert present["resolution_at"] == dt.isoformat()


def test_rules_block_resolution_at_string() -> None:
    present = _market_rules_block(
        {"id": "k", "resolution_at": "2026-07-01", "payload": {}}, ref="k"
    )
    assert present["resolution_at"] == "2026-07-01"


def test_compare_deadlines_same_day_and_no_pair() -> None:
    focal = {"resolution_at": "2026-07-01T10:00:00Z"}
    equiv = {"resolution_at": "2026-07-01T20:00:00Z"}
    cmp = _compare_deadlines(focal, equiv, focal_venue="kalshi", pair={"confidence": 1.0, "method": "m"})
    assert cmp["same_deadline_day"] is True
    assert cmp["confidence"] == 1.0
    # no pair → confidence/method None
    cmp2 = _compare_deadlines(focal, None, focal_venue="kalshi", pair=None)
    assert cmp2["confidence"] is None


def test_compare_deadlines_polymarket_focal_diff_day() -> None:
    focal = {"resolution_at": "2026-07-01T10:00:00Z"}
    equiv = {"resolution_at": "2026-07-05T10:00:00Z"}
    cmp = _compare_deadlines(focal, equiv, focal_venue="polymarket", pair=None)
    assert cmp["same_deadline_day"] is False
    assert cmp["deadlines"]["polymarket"] == "2026-07-01T10:00:00Z"


class _RulesIndex:
    def __init__(self) -> None:
        self.pairs_loaded = 7
        self.dataset_version = "v1"
        self.file_missing = False
        self.load_error: str | None = None
        self._map: dict[str, tuple[list[dict[str, Any]], str]] = {}

    def register(self, ref: str, pairs: list[dict[str, Any]], via: str) -> None:
        self._map[ref] = (pairs, via)

    def lookup(self, ref: str) -> tuple[list[dict[str, Any]], str]:
        return self._map.get(ref, ([], "none"))


class _RulesDao:
    def __init__(self, store: dict[str, dict[str, Any]]) -> None:
        self._store = store

    async def fetch_market(self, ref: str) -> dict[str, Any] | None:
        return self._store.get(ref)


async def test_rules_handler_full_pair() -> None:
    idx = _RulesIndex()
    idx.register("kalshi:A",
                 [{"pm_ref": "polymarket:1", "kalshi_title": "Kt", "pm_title": "Pt",
                   "confidence": 1.0, "method": "structured_key"}],
                 "kalshi_ticker")
    store = {
        "kalshi:A": {"id": "kalshi:A", "venue": "kalshi", "question": "Kq",
                     "resolution_at": "2026-07-01T00:00:00Z", "url": "k",
                     "payload": {"description": "K rules"}},
        "polymarket:1": {"id": "polymarket:1", "venue": "polymarket", "question": "Pq",
                         "resolution_at": "2026-07-01T12:00:00Z", "url": "p",
                         "payload": {"description": "P rules"}},
    }
    _, body = await handle_market_rules("kalshi:A", {}, dao=_RulesDao(store), equivalence=idx)
    assert body["market"]["resolution"] == "K rules"
    assert body["equivalent"]["resolution"] == "P rules"
    assert body["comparison"]["same_deadline_day"] is True


async def test_rules_handler_no_pair_degraded() -> None:
    idx = _RulesIndex()
    idx.file_missing = True
    _, body = await handle_market_rules("kalshi:X", {}, dao=_RulesDao({}), equivalence=idx)
    assert body["equivalent"] is None
    assert body["meta"]["degraded"] is True


async def test_rules_handler_load_error_meta() -> None:
    idx = _RulesIndex()
    idx.load_error = "boom"
    _, body = await handle_market_rules("kalshi:X", {}, dao=_RulesDao({}), equivalence=idx)
    assert body["meta"]["degraded_reason"] == "boom"


async def test_rules_handler_polymarket_focal_no_store() -> None:
    idx = _RulesIndex()
    idx.register("polymarket:1",
                 [{"kalshi_ref": "kalshi:A", "kalshi_title": "Kt", "pm_title": "Pt",
                   "confidence": 0.9, "method": "award_match"}],
                 "pm_gamma_id")
    # counterpart kalshi:A absent from store → equiv block from export title.
    _, body = await handle_market_rules(
        "polymarket:1", {}, dao=_RulesDao({}), equivalence=idx
    )
    assert body["equivalent"]["question"] == "Kt"
    assert body["equivalent"]["venue"] == "kalshi"


# =========================================================================== #
# status
# =========================================================================== #



def test_is_stale() -> None:
    assert _is_stale(None) is False
    assert _is_stale("not-a-date") is False
    old = (datetime.now(UTC) - timedelta(days=2)).isoformat()
    assert _is_stale(old) is True
    fresh = datetime.now(UTC).isoformat()
    assert _is_stale(fresh) is False
    # naive datetime string treated as UTC
    naive_old = (datetime.now(UTC) - timedelta(days=2)).replace(tzinfo=None).isoformat()
    assert _is_stale(naive_old) is True


def test_get_version_returns_str() -> None:
    assert isinstance(_get_version(), str)


@pytest.fixture(autouse=True)
def _reset_status_cache():
    status_mod._cache = None
    yield
    status_mod._cache = None


class _StatsDao:
    def __init__(self, rows: Any, *, raises: bool = False) -> None:
        self._rows = rows
        self._raises = raises

    async def fetch_venue_stats(self) -> Any:
        if self._raises:
            raise RuntimeError("db down")
        return self._rows


class _EqStub:
    pairs_loaded = 42
    dataset_version = "vX"


class _RelStub:
    pairs_loaded = 11


async def test_status_with_platforms() -> None:
    dt = datetime.now(UTC)
    dao = _StatsDao([
        {"venue": "kalshi", "count": 100, "last_updated": dt},
        {"venue": "polymarket", "count": 200,
         "last_updated": (dt - timedelta(days=2)).isoformat()},
    ])
    _, body = await handle_status({}, dao=dao, equivalence=_EqStub(), related=_RelStub())
    assert body["platforms"]["kalshi"]["status"] == "ok"
    assert body["platforms"]["polymarket"]["status"] == "stale"
    assert body["equivalence"]["pairs_loaded"] == 42
    assert body["related"]["pairs_loaded"] == 11


async def test_status_dao_no_method_omits_platforms() -> None:
    class _Bare:
        pass

    _, body = await handle_status({}, dao=_Bare(), equivalence=_EqStub(), related=_RelStub())
    assert "platforms" not in body


async def test_status_stats_exception_degrades() -> None:
    _, body = await handle_status(
        {}, dao=_StatsDao(None, raises=True), equivalence=_EqStub(), related=_RelStub()
    )
    assert "platforms" not in body
    assert body["service"]["version"]


async def test_status_cache_hit() -> None:
    dao = _StatsDao([])
    _, b1 = await handle_status({}, dao=dao, equivalence=_EqStub(), related=_RelStub())
    _, b2 = await handle_status({}, dao=dao, equivalence=_EqStub(), related=_RelStub())
    assert b1 is b2


async def test_status_lazy_singletons_swallow_import(monkeypatch) -> None:
    # Force the lazy-load branch (equivalence/related=None). The real singletons
    # load fine here; just assert the handler returns the blocks.
    _, body = await handle_status({}, dao=_StatsDao([]))
    assert "equivalence" in body and "related" in body


# =========================================================================== #
# markets_get
# =========================================================================== #



class _GetIndex:
    pairs_loaded = 3

    def lookup(self, ref: str) -> tuple[list[dict[str, Any]], str]:
        if ref == "kalshi:KNOWN":
            return [{"pm_ref": "polymarket:1"}], "kalshi_ticker"
        return [], "none"


class _GetDao:
    def __init__(self, store: dict[str, dict[str, Any]]) -> None:
        self._store = store

    async def fetch_market(self, ref: str) -> dict[str, Any] | None:
        return self._store.get(ref)


async def test_get_raw_ticker_fallback_to_kalshi_prefix() -> None:
    store = {"kalshi:RAW": {"id": "kalshi:RAW", "venue": "kalshi", "question": "Q?",
                            "status": "active", "payload": {}}}
    _, body = await handle_market_get("RAW", {}, dao=_GetDao(store), equivalence=_GetIndex())
    assert body["market"]["found"] is True
    assert body["market"]["venue"] == "kalshi"


# =========================================================================== #
# ref_utils
# =========================================================================== #



def test_normalize_ref_non_string_passthrough() -> None:
    assert normalize_ref(123) == 123  # type: ignore[arg-type]


def test_normalize_ref_blank() -> None:
    assert normalize_ref("   ") == ""


def test_normalize_ref_kalshi_url() -> None:
    assert normalize_ref("https://kalshi.com/markets/KXFOO-26") == "kalshi:KXFOO-26"
    assert normalize_ref("https://www.kalshi.com/events/KXBAR/") == "kalshi:KXBAR"


def test_normalize_ref_polymarket_url() -> None:
    assert normalize_ref("https://polymarket.com/event/some-slug") == "polymarket:some-slug"
    assert normalize_ref("https://polymarket.com/market/other-slug?tid=1") == "polymarket:other-slug"


def test_normalize_ref_unrecognized_url_passthrough() -> None:
    assert normalize_ref("https://example.com/foo") == "https://example.com/foo"
    # kalshi host but no matching path → returned as-is
    assert normalize_ref("https://kalshi.com/about") == "https://kalshi.com/about"


def test_normalize_ref_prefix_casefold() -> None:
    assert normalize_ref("Kalshi:ABC") == "kalshi:ABC"
    assert normalize_ref("POLYMARKET:slug") == "polymarket:slug"
    assert normalize_ref("plain-ticker") == "plain-ticker"


# =========================================================================== #
# gate internals
# =========================================================================== #



def test_client_ip_xff_and_peer() -> None:
    scope_xff = {"headers": [(b"x-forwarded-for", b"1.2.3.4, 5.6.7.8")]}
    assert _client_ip(scope_xff) == "1.2.3.4"
    scope_peer = {"headers": [], "client": ("9.9.9.9", 1234)}
    assert _client_ip(scope_peer) == "9.9.9.9"
    scope_none = {"headers": []}
    assert _client_ip(scope_none) == "unknown"


async def test_gate_non_http_passthrough() -> None:
    seen = {"called": False}

    async def _inner(scope, receive, send):
        seen["called"] = True

    gate = ApiGate(_inner, require_api_key=True, api_keys=frozenset({"k"}),
                   rate_per_min=10.0, burst=5.0)
    await gate({"type": "lifespan"}, None, None)
    assert seen["called"] is True


def test_gate_prune_evicts_refilled_buckets() -> None:
    gate = ApiGate(lambda *a: None, require_api_key=False, api_keys=frozenset(),
                   rate_per_min=60.0, burst=5.0)
    # seed a bucket then prune with a 'now' far in the future → evicted.
    gate._allow("ip:1.1.1.1")
    assert "ip:1.1.1.1" in gate._buckets
    gate._prune(time.monotonic() + 10_000.0)
    assert "ip:1.1.1.1" not in gate._buckets


def test_gate_prune_zero_rate_no_crash() -> None:
    gate = ApiGate(lambda *a: None, require_api_key=False, api_keys=frozenset(),
                   rate_per_min=0.0, burst=5.0)
    gate._buckets["x"] = gate._buckets.get("x") or _seed(gate)
    gate._prune(time.monotonic())  # rate_per_sec==0 → refill_s 0.0, must not divide-by-zero


def _seed(gate: ApiGate):
    from pytheum.api.gate import _Bucket
    return _Bucket(1.0, time.monotonic())


# =========================================================================== #
# api/__init__ register wiring
# =========================================================================== #



def test_register_all_wires_routes() -> None:
    reg = RouterRegistry()
    register_all(reg, dao=None, equivalence=None, related=None, clients=None)
    paths = {pattern for (_method, pattern) in reg._specs}
    assert "/v1/status" in paths
    assert "/v1/markets/matched" in paths
    assert "/v1/markets/screen" in paths
    assert "/v1/markets/{ref}/core" in paths


def test_register_groups_independently() -> None:
    reg = RouterRegistry()
    register_group_A(reg, dao=None)
    register_group_B(reg, clients=None, dao=None)
    paths = {pattern for (_method, pattern) in reg._specs}
    assert "/v1/traders/{wallet}" in paths


async def test_dispatch_group_a_closures() -> None:
    """Drive the Group A handler closures through a built router so the
    closure bodies (status/metrics/equivalents/matched/rules/get/related) run."""
    reg = RouterRegistry()
    register_group_A(reg, dao=None, equivalence=None, related=None)
    router = reg.build_router()
    for method, path in [
        ("GET", "/v1/status"),
        ("GET", "/v1/metrics"),
        ("GET", "/v1/markets/matched"),
        ("GET", "/v1/markets/kalshi:NOPE/core"),
        ("GET", "/v1/markets/kalshi:NOPE/rules"),
        ("GET", "/v1/markets/kalshi:NOPE/related"),
        ("GET", "/v1/markets/kalshi:NOPE/equivalents"),
    ]:
        result = await router.dispatch(method, path, {})
        assert result is not None
        status, _ = result
        assert status == 200


async def test_dispatch_group_b_closures() -> None:
    """Drive the Group B handler closures (screen/search/trader-data) with
    clients=None / dao=None so the closures execute their degraded paths."""
    reg = RouterRegistry()
    register_group_B(reg, clients=None, dao=None)
    router = reg.build_router()
    for path in [
        "/v1/markets/screen",
        "/v1/markets/search",
        "/v1/markets/whale-trades",
        "/v1/markets/kalshi:X/book",
        "/v1/markets/kalshi:X/trades",
        "/v1/markets/kalshi:X/oi",
        "/v1/markets/kalshi:X/ohlcv",
        "/v1/markets/polymarket:1/holders",
        "/v1/traders/leaderboard",
        "/v1/traders/0xWALLET",
    ]:
        result = await router.dispatch("GET", path, {})
        assert result is not None
        status, _ = result
        assert isinstance(status, int)


# =========================================================================== #
# markets_rules fallback branches
# =========================================================================== #


async def test_rules_same_day_compare_handles_bad_value() -> None:
    """A non-subscriptable resolution_at on one side → same_day None, no crash."""
    cmp = _compare_deadlines(
        {"resolution_at": 12345},  # not a string → slicing raises TypeError
        {"resolution_at": "2026-07-01T00:00:00Z"},
        focal_venue="kalshi",
        pair=None,
    )
    assert cmp["same_deadline_day"] is None


async def test_rules_polymarket_condition_id_fallback() -> None:
    idx = _RulesIndex()
    idx.register(
        "polymarket:0xBEEF",
        [{"kalshi_ref": "kalshi:A", "kalshi_title": "Kt", "pm_title": "Pt",
          "pm_ref": "polymarket:5", "confidence": 1.0, "method": "award_match"}],
        "pm_condition_id",
    )
    # focal hydrates via the canonical pm_ref fallback (0xBEEF absent from store).
    store = {
        "polymarket:5": {"id": "polymarket:5", "venue": "polymarket", "question": "Pq",
                         "resolution_at": "2026-07-01T00:00:00Z", "payload": {}},
        "kalshi:A": {"id": "kalshi:A", "venue": "kalshi", "question": "Kq",
                     "resolution_at": "2026-07-01T00:00:00Z", "payload": {}},
    }
    _, body = await handle_market_rules(
        "polymarket:0xBEEF", {}, dao=_RulesDao(store), equivalence=idx
    )
    assert body["market"]["id"] == "polymarket:5"


async def test_rules_raw_kalshi_ticker_fallback() -> None:
    idx = _RulesIndex()
    idx.register(
        "KX-RAW",
        [{"pm_ref": "polymarket:7", "kalshi_title": "Kt", "pm_title": "Pt",
          "confidence": 1.0, "method": "structured_key"}],
        "kalshi_ticker",
    )
    store = {
        "kalshi:KX-RAW": {"id": "kalshi:KX-RAW", "venue": "kalshi", "question": "Kq",
                          "resolution_at": "2026-07-01T00:00:00Z", "payload": {}},
        "polymarket:7": {"id": "polymarket:7", "venue": "polymarket", "question": "Pq",
                         "resolution_at": "2026-07-01T00:00:00Z", "payload": {}},
    }
    _, body = await handle_market_rules(
        "KX-RAW", {}, dao=_RulesDao(store), equivalence=idx
    )
    assert body["market"]["id"] == "kalshi:KX-RAW"


# =========================================================================== #
# markets_get no-dao + absent
# =========================================================================== #


async def test_get_no_dao_degrades() -> None:
    _, body = await handle_market_get("kalshi:X", {}, dao=None, equivalence=_GetIndex())
    assert body["market"]["found"] is False
    assert body["meta"]["degraded"] is True


async def test_get_has_equivalent_flag() -> None:
    _, body = await handle_market_get(
        "kalshi:KNOWN", {}, dao=_GetDao({}), equivalence=_GetIndex()
    )
    assert body["meta"]["has_equivalent"] is True
    assert body["meta"]["matched_via"] == "kalshi_ticker"


async def test_matched_hydrates_pm_leg_via_condition_id() -> None:
    """Regression (2026-07-03): PM leg must hydrate when the store keys PM by the
    condition-id form, not the gamma pm_ref. Before the fix the PM leg was always
    None → empty net_edge/spread arb radar on /v1/markets/matched."""
    idx = _matched_index([{
        "kalshi_ref": "kalshi:KX-A", "kalshi_ticker": "KX-A",
        "pm_ref": "polymarket:2702712", "pm_gamma_id": "2702712",
        "pm_condition_id": "0xABC", "bet_type": "moneyline",
        "method": "structured_key", "confidence": 1.0,
        "kalshi_title": "kt", "pm_title": "pt",
    }])
    # store keys PM by the condition-prefixed markets.id — NOT the gamma pm_ref
    store = {
        "kalshi:KX-A": {"id": "kalshi:KX-A", "venue": "kalshi", "status": "active",
                        "volume_usd": 100.0,
                        "payload": {"outcomePrices": "[0.5,0.5]", "bestBid": "0.10", "bestAsk": "0.20"}},
        "polymarket:0xABC": {"id": "polymarket:0xABC", "venue": "polymarket", "status": "active",
                             "volume_usd": 50.0,
                             "payload": {"outcomePrices": "[0.5,0.5]", "bestBid": "0.85", "bestAsk": "0.95"}},
    }
    _, body = await handle_markets_matched({}, dao=_BatchDao(store), equivalence=idx)
    pm = body["pairs"][0]["polymarket"]
    assert pm["implied_yes"] is not None, "PM leg must hydrate via condition_id"
    assert "net_edge" in body["pairs"][0]["cross_venue"], "arb radar computes with both legs booked"


async def test_matched_pm_leg_still_hydrates_via_gamma_ref() -> None:
    """Back-compat: if the store DOES key PM by the gamma pm_ref, that still works."""
    idx = _matched_index([_pair("A", "1")])
    store = {
        "kalshi:A": {"id": "kalshi:A", "venue": "kalshi", "status": "active", "volume_usd": 1.0,
                     "payload": {"outcomePrices": "[0.5,0.5]", "bestBid": "0.10", "bestAsk": "0.20"}},
        "polymarket:1": {"id": "polymarket:1", "venue": "polymarket", "status": "active", "volume_usd": 1.0,
                         "payload": {"outcomePrices": "[0.5,0.5]", "bestBid": "0.85", "bestAsk": "0.95"}},
    }
    _, body = await handle_markets_matched({}, dao=_BatchDao(store), equivalence=idx)
    assert body["pairs"][0]["polymarket"]["implied_yes"] is not None


async def test_matched_resolved_leg_excluded_and_sorts_last() -> None:
    """PM keeps status='active' on resolved markets → a status-only is_live let
    settled pairs lead the net_edge radar with phantom edges. The resolution_at
    guard fixes it: resolved pairs are not-live (sort last), and live_only prunes them."""
    from datetime import datetime, timedelta, timezone
    now = datetime.now(timezone.utc)
    past = (now - timedelta(days=3)).isoformat()
    future = (now + timedelta(days=30)).isoformat()
    idx = _matched_index([_pair("LIVE", "1"), _pair("DEAD", "2")])
    # DEAD: PM leg still status='active' (the quirk) but resolved in the past.
    store = {
        "kalshi:LIVE": {"id": "kalshi:LIVE", "status": "active", "resolution_at": future,
                        "volume_usd": 1.0,
                        "payload": {"outcomePrices": "[0.5,0.5]", "bestBid": "0.10", "bestAsk": "0.20"}},
        "polymarket:1": {"id": "polymarket:1", "status": "active", "resolution_at": future,
                         "volume_usd": 1.0,
                         "payload": {"outcomePrices": "[0.5,0.5]", "bestBid": "0.85", "bestAsk": "0.95"}},
        "kalshi:DEAD": {"id": "kalshi:DEAD", "status": "active", "resolution_at": past,
                        "volume_usd": 9e9,  # huge lifetime volume — would dominate a naive sort
                        "payload": {"outcomePrices": "[0.5,0.5]", "bestBid": "0.66", "bestAsk": "0.68"}},
        "polymarket:2": {"id": "polymarket:2", "status": "active", "resolution_at": past,
                         "volume_usd": 9e9,
                         "payload": {"outcomePrices": "[0.5,0.5]", "bestBid": "0.9990", "bestAsk": "0.9999"}},
    }
    # default (live_only off): both returned, but LIVE sorts first, DEAD is not-live
    _, body = await handle_markets_matched({"sort_by": "net_edge"}, dao=_BatchDao(store), equivalence=idx)
    ids = [p["kalshi"]["id"] for p in body["pairs"]]
    assert ids[0] == "kalshi:LIVE", f"live pair must lead the radar, got {ids}"
    dead = next(p for p in body["pairs"] if p["kalshi"]["id"] == "kalshi:DEAD")
    assert dead["is_live"] is False, "resolved-but-status-active pair must read not-live"
    # live_only=true: DEAD pruned entirely
    _, body2 = await handle_markets_matched(
        {"sort_by": "net_edge", "live_only": "true"}, dao=_BatchDao(store), equivalence=idx)
    assert [p["kalshi"]["id"] for p in body2["pairs"]] == ["kalshi:LIVE"]
    assert body2["meta"]["filter"]["live_only"] is True
