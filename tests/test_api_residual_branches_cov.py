"""Residual-branch coverage for the smaller API modules.

Targets the last uncovered lines flagged by --cov-report=term-missing:

- markets_equivalents: DAO-fallback pairs source, warm-loop timeout-continue,
  unknown focal-venue (no counterpart) path.
- markets_matched: empty-token skip in _parse_bet_type_filter, load_error
  degraded meta.
- markets_rules: unknown focal-venue counterpart branch.
- gate: prune-on-first-request + hard-cap eviction.
- status: version-lookup exception, equivalence/related singleton import failure.
- ref_utils: urlparse exception, scheme-less ref, polymarket path no-match.
"""
from __future__ import annotations

import asyncio
from typing import Any

import pytest

from pytheum.api import markets_equivalents as eqmod
from pytheum.api import status as status_mod
from pytheum.api.gate import ApiGate
from pytheum.api.markets_equivalents import (
    handle_market_equivalents,
    handle_markets_equivalents,
    warm_equivalents_loop,
)
from pytheum.api.markets_matched import _parse_bet_type_filter, handle_markets_matched
from pytheum.api.markets_rules import handle_market_rules
from pytheum.api.ref_utils import _extract_from_url, normalize_ref
from pytheum.api.status import _get_version, handle_status


@pytest.fixture(autouse=True)
def _clear_eq_cache() -> Any:
    eqmod._cache.clear()
    yield
    eqmod._cache.clear()


# --------------------------------------------------------------------------- #
# Fakes
# --------------------------------------------------------------------------- #


class _PairsDao:
    """DAO that serves equivalence pairs directly (no index supplied)."""

    def __init__(self, pairs: list[dict[str, Any]], markets: dict[str, dict[str, Any]]) -> None:
        self._pairs = pairs
        self._markets = markets

    async def fetch_equivalence_pairs(self, *, limit: int) -> list[dict[str, Any]]:
        return self._pairs[:limit]

    async def fetch_markets_by_ids(self, ids: list[str]) -> list[dict[str, Any]]:
        return [self._markets[i] for i in ids if i in self._markets]


def _leg(mid: str, venue: str) -> dict[str, Any]:
    return {
        "id": mid, "venue": venue, "question": f"q-{mid}", "status": "active",
        "volume_usd": 100.0, "liquidity_usd": 10.0, "url": f"https://x/{mid}",
        "resolution_at": None,
        "payload": {"outcomePrices": "[\"0.4\", \"0.6\"]", "bestBid": "0.39", "bestAsk": "0.61"},
    }


# --------------------------------------------------------------------------- #
# markets_equivalents collection — DAO fallback path (lines 208-211)
# --------------------------------------------------------------------------- #


async def test_collection_dao_fallback_source(monkeypatch: pytest.MonkeyPatch) -> None:
    # Force the lazy singleton to None so the DAO fallback branch (208-211) runs.
    import pytheum.equivalence.index as eqidx
    monkeypatch.setattr(eqidx, "get_index", lambda: None)
    pairs = [{
        "kalshi_market_id": "kalshi:K1", "polymarket_market_id": "polymarket:1",
        "method": "structured_key", "confidence": 1.0, "bet_type": "moneyline",
        "poly_side": 0, "poly_outcome": "Yes",
    }]
    markets = {"kalshi:K1": _leg("kalshi:K1", "kalshi"),
               "polymarket:1": _leg("polymarket:1", "polymarket")}
    dao = _PairsDao(pairs, markets)
    status, body = await handle_markets_equivalents({"limit": "10"}, dao=dao)
    assert status == 200
    assert body["count"] == 1
    assert body["pairs"][0]["a"]["venue"] == "kalshi"


async def test_collection_no_source_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    # Singleton None AND dao=None → no source → empty pairs (else branch, 211).
    import pytheum.equivalence.index as eqidx
    monkeypatch.setattr(eqidx, "get_index", lambda: None)
    status, body = await handle_markets_equivalents({"limit": "5"}, dao=None)
    assert status == 200
    assert body["count"] == 0
    assert body["pairs"] == []


# --------------------------------------------------------------------------- #
# warm loop timeout-continue (lines 283-284)
# --------------------------------------------------------------------------- #


async def test_warm_loop_timeout_continue(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = {"n": 0}

    class _Stop:
        def is_set(self) -> bool:
            # run exactly one full cycle, then stop on the 2nd is_set check.
            calls["n"] += 1
            return calls["n"] > 1

        async def wait(self) -> None:  # never returns within the timeout
            await asyncio.sleep(10)

    async def _noop_handler(*a: Any, **k: Any) -> tuple[int, dict[str, Any]]:
        return 200, {}

    monkeypatch.setattr(eqmod, "handle_markets_equivalents", _noop_handler)
    monkeypatch.setattr(eqmod, "_WARM_INTERVAL_S", 0.001)
    monkeypatch.setattr(eqmod, "_WARM_KEYS", (10,))
    await warm_equivalents_loop(dao=object(), stop=_Stop())
    assert calls["n"] >= 2  # looped at least once then exited


# --------------------------------------------------------------------------- #
# per-ref handler unknown focal venue (lines 443-444, 451)
# --------------------------------------------------------------------------- #


class _ManifoldIndex:
    """Returns a pair but a matched_via outside _MATCHED_VIA_VENUE so focal_venue
    stays None and counterpart_ref_key is empty (the else branch)."""

    pairs_loaded = 1
    dataset_version = "v"
    file_missing = False
    load_error = None

    def lookup(self, ref: str) -> tuple[list[dict[str, Any]], str]:
        return [{"kalshi_title": "kt", "pm_title": "pt", "pm_ref": "polymarket:1"}], "exotic_via"


class _NoMarketDao:
    async def fetch_market(self, ref: str) -> dict[str, Any] | None:
        return None


async def test_per_ref_unknown_focal_venue() -> None:
    status, body = await handle_market_equivalents(
        "manifold:x", {}, dao=_NoMarketDao(), equivalence=_ManifoldIndex()
    )
    assert status == 200
    # counterpart_ref_key was "" → no equivalents appended (line 451 continue).
    assert body["equivalents"] == []


async def test_rules_unknown_focal_venue() -> None:
    status, body = await handle_market_rules(
        "manifold:x", {}, dao=_NoMarketDao(), equivalence=_ManifoldIndex()
    )
    assert status == 200
    # else branch sets counterpart_ref="" → equiv_block stays None.
    assert body.get("equivalent") is None


# --------------------------------------------------------------------------- #
# _parse_bet_type_filter empty-token skip (line 122)
# --------------------------------------------------------------------------- #


def test_parse_bet_type_filter_empty_token() -> None:
    out = _parse_bet_type_filter(
        "moneyline, ,totals", groups={}, available={"moneyline", "totals"}
    )
    assert out == {"moneyline", "totals"}


def test_parse_bet_type_filter_group_expansion() -> None:
    out = _parse_bet_type_filter(
        "sports", groups={"sports": {"moneyline", "spread"}}, available=set()
    )
    assert out == {"moneyline", "spread"}


def test_parse_bet_type_filter_none() -> None:
    assert _parse_bet_type_filter(None, groups={}, available=set()) is None
    assert _parse_bet_type_filter(" , ", groups={}, available=set()) is None


# --------------------------------------------------------------------------- #
# markets_matched load_error degraded meta (lines 401-402)
# --------------------------------------------------------------------------- #


class _LoadErrorIndex:
    BET_TYPE_GROUPS: dict[str, set[str]] = {}
    bet_types_available: list[str] = []
    pairs_loaded = 0
    file_missing = False
    load_error = "disk on fire"

    def browse(self, **kw: Any) -> tuple[list[dict[str, Any]], int]:
        return [], 0


class _EmptyDao:
    async def fetch_markets_by_ids(self, ids: list[str]) -> list[dict[str, Any]]:
        return []


async def test_matched_load_error_degraded() -> None:
    status, body = await handle_markets_matched(
        {}, dao=_EmptyDao(), equivalence=_LoadErrorIndex()
    )
    assert status == 200
    assert body["meta"]["degraded"] is True
    assert body["meta"]["degraded_reason"] == "disk on fire"


class _BrowseIndex:
    """Returns one pair carrying a BARE kalshi_ticker (no 'kalshi:' prefix) so
    the prefix-normalisation branch (line 301) runs, plus a hydrated leg."""

    BET_TYPE_GROUPS: dict[str, set[str]] = {}
    bet_types_available = ["moneyline"]
    pairs_loaded = 1
    dataset_version = "v"
    file_missing = False
    load_error = None

    def browse(self, **kw: Any) -> tuple[list[dict[str, Any]], int]:
        return [{
            "kalshi_ticker": "K1",  # bare → must be prefixed to kalshi:K1
            "pm_ref": "polymarket:1",
            "kalshi_title": "kt", "pm_title": "pt",
            "bet_type": "moneyline", "confidence": 1.0, "method": "structured_key",
        }], 1

    def leagues_available(self, *, max_values: int = 50) -> list[str]:
        return []


class _TwoLegDao:
    def __init__(self, store: dict[str, dict[str, Any]]) -> None:
        self._store = store

    async def fetch_markets_by_ids(self, ids: list[str]) -> list[dict[str, Any]]:
        return [self._store[i] for i in ids if i in self._store]


async def test_matched_bare_kalshi_ticker_prefixed() -> None:
    store = {"kalshi:K1": _leg("kalshi:K1", "kalshi"),
             "polymarket:1": _leg("polymarket:1", "polymarket")}
    status, body = await handle_markets_matched(
        {}, dao=_TwoLegDao(store), equivalence=_BrowseIndex()
    )
    assert status == 200
    assert body["total"] == 1
    assert body["pairs"][0]["kalshi"]["id"] == "kalshi:K1"


async def test_matched_min_volume_filters_below_threshold() -> None:
    # k_vol below threshold → pair skipped (min_volume branch + overfetch page).
    store = {"kalshi:K1": _leg("kalshi:K1", "kalshi") | {"volume_usd": 5.0},
             "polymarket:1": _leg("polymarket:1", "polymarket")}
    status, body = await handle_markets_matched(
        {"min_volume": "1000"}, dao=_TwoLegDao(store), equivalence=_BrowseIndex()
    )
    assert status == 200
    assert body["pairs"] == []


# --------------------------------------------------------------------------- #
# gate prune + hard cap (lines 131, 148-150)
# --------------------------------------------------------------------------- #


async def _inner_app(scope: dict[str, Any], receive: Any, send: Any) -> None:
    return None


def _gate() -> ApiGate:
    return ApiGate(
        _inner_app,
        require_api_key=False,
        api_keys=frozenset(),
        rate_per_min=60.0,
        burst=2.0,
    )


def test_gate_prune_triggers_on_first_request() -> None:
    gate = _gate()
    # Seed > 50k buckets so the first-request branch calls _prune (line 131).
    import time as _t

    from pytheum.api.gate import _Bucket
    now = _t.monotonic()
    for i in range(50_001):
        # last far in the past so they're considered stale and get pruned.
        gate._buckets[f"old-{i}"] = _Bucket(2.0, now - 10_000)
    allowed = gate._allow("brand-new-client")
    assert allowed is True
    # stale buckets pruned, fresh client retained.
    assert "brand-new-client" in gate._buckets
    assert len(gate._buckets) < 50_001


def test_gate_prune_hard_cap_eviction() -> None:
    gate = _gate()
    import time as _t

    from pytheum.api.gate import _Bucket
    now = _t.monotonic()
    # All "fresh" (last == now) so the stale sweep removes none → hard-cap path
    # (lines 147-150) evicts the oldest down to 50_000.
    for i in range(50_010):
        gate._buckets[f"c-{i}"] = _Bucket(2.0, now)
    gate._prune(now)
    assert len(gate._buckets) == 50_000


# --------------------------------------------------------------------------- #
# status — version exception + singleton import failure (77-78, 108-109, 115-116)
# --------------------------------------------------------------------------- #


def test_get_version_exception_returns_dev(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(status_mod, "_service_version", None)
    import importlib.metadata as md

    def _boom(name: str) -> str:
        raise md.PackageNotFoundError(name)

    monkeypatch.setattr(md, "version", _boom)
    assert _get_version() == "dev"


class _NoStatsDao:
    pass  # no fetch_venue_stats → platforms block omitted


async def test_status_singleton_import_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    status_mod._cache = None
    # Force both lazy singleton imports to raise → equivalence/related = None.
    import builtins
    real_import = builtins.__import__

    def _fake_import(name: str, *a: Any, **k: Any) -> Any:
        if name in ("pytheum.equivalence.index", "pytheum.related.index"):
            raise ImportError("no module")
        return real_import(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", _fake_import)
    status, body = await handle_status({}, dao=_NoStatsDao())
    monkeypatch.undo()
    assert status == 200
    assert body["equivalence"]["pairs_loaded"] == 0
    assert body["related"]["pairs_loaded"] == 0
    assert "platforms" not in body
    status_mod._cache = None


# --------------------------------------------------------------------------- #
# ref_utils residuals (43-44, 46, 66)
# --------------------------------------------------------------------------- #


def test_extract_from_url_urlparse_exception(monkeypatch: pytest.MonkeyPatch) -> None:
    import pytheum.api.ref_utils as ru

    def _boom(x: str) -> Any:
        raise ValueError("bad url")

    monkeypatch.setattr(ru, "urlparse", _boom)
    assert _extract_from_url("http://kalshi.com/markets/X") is None


def test_extract_from_url_no_scheme() -> None:
    # No scheme → returns None (line 46).
    assert _extract_from_url("kalshi.com/markets/X") is None


def test_extract_from_url_pm_path_no_match() -> None:
    # polymarket host but path doesn't match event|market pattern (line 66).
    assert _extract_from_url("https://polymarket.com/profile/abc") is None


def test_extract_from_url_kalshi_path_no_match() -> None:
    assert _extract_from_url("https://kalshi.com/about") is None


def test_normalize_ref_scheme_less_passthrough() -> None:
    # bare host without scheme is not a URL → prefix case-fold path.
    assert normalize_ref("Kalshi:ABC") == "kalshi:ABC"


def test_normalize_ref_unrecognized_url_passthrough() -> None:
    assert normalize_ref("https://example.com/foo") == "https://example.com/foo"


# --------------------------------------------------------------------------- #
# markets_screen _attach_bundle_top_outcome apply-loop skip (line 91)
# --------------------------------------------------------------------------- #


class _ChildrenOnlyDao:
    def __init__(self, children: dict[str, list[dict[str, Any]]]) -> None:
        self._children = children

    async def fetch_children_for_events(
        self, event_ids: list[str]
    ) -> dict[str, list[dict[str, Any]]]:
        return self._children


async def test_bundle_apply_loop_skips_priced_row() -> None:
    from pytheum.api.markets_screen import _attach_bundle_top_outcome

    # One unpriced PM event-parent (collected into event_ids) PLUS a priced PM
    # row that the apply-loop must skip via the `implied_yes is not None` branch
    # (line 90-91 continue) without mislabeling it as a bundle.
    parent: dict[str, Any] = {
        "id": "polymarket:evt-1", "venue": "polymarket", "implied_yes": None,
    }
    priced: dict[str, Any] = {
        "id": "polymarket:m-2", "venue": "polymarket", "implied_yes": 0.62,
    }
    markets: list[dict[str, Any]] = [parent, priced]
    children = {"evt-1": [
        {"outcome": "A", "implied_yes": 0.7},
        {"outcome": "B", "implied_yes": 0.3},
    ]}
    await _attach_bundle_top_outcome(markets, dao=_ChildrenOnlyDao(children))
    # Parent got bundle fields; the priced row was left untouched.
    assert "bundle_top_outcome" in parent
    assert "bundle_top_outcome" not in priced
