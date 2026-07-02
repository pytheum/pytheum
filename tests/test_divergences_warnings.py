"""find_divergences honesty surface: warnings, demotion ordering, lockable notional.

The 2026-06-29 self-probe found (a) resolution_mismatch pairs still ranked by edge
with the flag buried, (b) top hits were 0.14-day-to-resolution markets with 24-40pt
"edges" on stale legs (the #1 flagged edge collapsed 23.1c -> -4c on requote), and
(c) no fillable-size signal, so a 2-contract "edge" read like a real one. Covered:

1. _row_warnings — each warning condition fires exactly on its trigger.
2. _lockable_notional — depth-capped notional on the edge's direction; null +
   depth_unverified when a leg's size is unknown (never guessed).
3. Default ordering — ANY warned row sorts after every clean row (edge order kept
   within each group).
4. include_warned=false — warned rows filtered out, counted in warned_filtered.
5. MCP wrapper — t_find_divergences forwards include_warned.
"""

from __future__ import annotations

import pytest

import pytheum.mcp.server as server
import pytheum.mcp.tools as tools
from pytheum.mcp.tools import (
    _NOTIONAL_BASIS_DEPTH,
    _NOTIONAL_BASIS_UNVERIFIED,
    _lockable_notional,
    _row_warnings,
    find_divergences,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# Two-sided books WITH top-of-book sizes so depth_unverified never fires unless a
# test removes the sizes on purpose. ~30pt gap -> a clear positive edge either way.
_K_BOOK = {"bid": 0.54, "ask": 0.56, "bid_size": 100.0, "ask_size": 200.0}
_P_BOOK = {"bid": 0.24, "ask": 0.26, "bid_size": 300.0, "ask_size": 400.0}


def _pair(*, k_days: float = 5.0, p_days: float = 5.0,
          k_book: dict | None = None, p_book: dict | None = None,
          k_age_s: float | None = None, p_age_s: float | None = None) -> dict:
    """Synthetic /v1/markets/equivalents pair shaped like the live endpoint's."""
    a = {"id": "kalshi:KXW", "venue": "kalshi", "question": "Will X happen?",
         "days_to_resolution": k_days, "book": dict(k_book or _K_BOOK)}
    b = {"id": "polymarket:1", "venue": "polymarket", "question": "Will X happen?",
         "days_to_resolution": p_days, "book": dict(p_book or _P_BOOK)}
    if k_age_s is not None:
        a["last_move_age_s"] = k_age_s
    if p_age_s is not None:
        b["last_move_age_s"] = p_age_s
    return {"bet_type": "event", "poly_side": None, "method": "opus_backstop",
            "confidence": 1.0, "a": a, "b": b}


def _patch_pairs(monkeypatch: pytest.MonkeyPatch, pairs: list[dict]) -> None:
    async def _fake_get(path, params, base_url):  # noqa: ANN001
        # main pass returns the pairs; dedicated bet_type passes empty (no dupes).
        return {"pairs": pairs} if not params.get("bet_type") else {"pairs": []}

    monkeypatch.setattr(tools, "_get", _fake_get)


# ---------------------------------------------------------------------------
# 1. _row_warnings — each condition
# ---------------------------------------------------------------------------


def test_row_warnings_clean_row_is_empty() -> None:
    legs = ({"days_to_resolution": 5.0, "last_move_age_s": 60.0},
            {"days_to_resolution": 5.0, "last_move_age_s": 45.0})
    assert _row_warnings(resolution_mismatch=False, either_leg_parked=False,
                         legs=legs, depth_unverified=False) == []


def test_row_warnings_resolution_mismatch() -> None:
    legs = ({"days_to_resolution": 14.0}, {"days_to_resolution": 5.0})
    w = _row_warnings(resolution_mismatch=True, either_leg_parked=False,
                      legs=legs, depth_unverified=False)
    assert w == ["resolution_mismatch"]


def test_row_warnings_either_leg_parked() -> None:
    legs = ({"days_to_resolution": 5.0}, {"days_to_resolution": 5.0})
    w = _row_warnings(resolution_mismatch=False, either_leg_parked=True,
                      legs=legs, depth_unverified=False)
    assert w == ["either_leg_parked"]


def test_row_warnings_stale_quote_over_threshold() -> None:
    legs = ({"days_to_resolution": 5.0, "last_move_age_s": 7200.0},
            {"days_to_resolution": 5.0, "last_move_age_s": 30.0})
    w = _row_warnings(resolution_mismatch=False, either_leg_parked=False,
                      legs=legs, depth_unverified=False)
    assert w == ["stale_quote"]


def test_row_warnings_stale_quote_not_fired_at_or_under_threshold() -> None:
    # boundary: exactly 3600s is NOT stale (> threshold, not >=); missing age = no data,
    # no warning (where-the-data-exists semantics).
    legs = ({"days_to_resolution": 5.0, "last_move_age_s": 3600.0},
            {"days_to_resolution": 5.0})
    assert _row_warnings(resolution_mismatch=False, either_leg_parked=False,
                         legs=legs, depth_unverified=False) == []


def test_row_warnings_near_resolution() -> None:
    # the probe's 0.14-day-to-resolution top hits must carry the label.
    legs = ({"days_to_resolution": 0.14}, {"days_to_resolution": 5.0})
    w = _row_warnings(resolution_mismatch=False, either_leg_parked=False,
                      legs=legs, depth_unverified=False)
    assert w == ["near_resolution"]


def test_row_warnings_depth_unverified() -> None:
    legs = ({"days_to_resolution": 5.0}, {"days_to_resolution": 5.0})
    w = _row_warnings(resolution_mismatch=False, either_leg_parked=False,
                      legs=legs, depth_unverified=True)
    assert w == ["depth_unverified"]


def test_row_warnings_multiple_conditions_all_listed() -> None:
    legs = ({"days_to_resolution": 0.14, "last_move_age_s": 7200.0},
            {"days_to_resolution": 5.0})
    w = _row_warnings(resolution_mismatch=True, either_leg_parked=True,
                      legs=legs, depth_unverified=True)
    assert w == ["resolution_mismatch", "either_leg_parked", "stale_quote",
                 "near_resolution", "depth_unverified"]


# ---------------------------------------------------------------------------
# 2. _lockable_notional
# ---------------------------------------------------------------------------


def _netted(book: dict, venue: str) -> dict:
    b = dict(book)
    tools._net_book(venue, b, bet_type="event")
    return b


def test_lockable_notional_both_legs_depth() -> None:
    """Cheaper direction here is buy YES on PM (ask 0.26) + buy NO on Kalshi (hit
    the 0.54 bid): PM leg fills 400 x 0.26 = 104.0; Kalshi NO leg fills
    100 x (1-0.54) = 46.0 -> min = 46.0 (the binding leg)."""
    k, p = _netted(_K_BOOK, "kalshi"), _netted(_P_BOOK, "polymarket")
    notional, basis = _lockable_notional(k, p)
    assert notional == 46.0
    assert basis == _NOTIONAL_BASIS_DEPTH


def test_lockable_notional_one_leg_missing_size_is_null() -> None:
    """Depth unknown on one leg -> null, never guessed."""
    k = _netted({"bid": 0.54, "ask": 0.56}, "kalshi")  # no sizes captured
    p = _netted(_P_BOOK, "polymarket")
    notional, basis = _lockable_notional(k, p)
    assert notional is None
    assert basis == _NOTIONAL_BASIS_UNVERIFIED


def test_lockable_notional_no_net_fields_is_null() -> None:
    # books that never went through _net_book (no direction computable)
    notional, basis = _lockable_notional(dict(_K_BOOK), dict(_P_BOOK))
    assert notional is None
    assert basis == _NOTIONAL_BASIS_UNVERIFIED


def test_lockable_notional_non_dict_books_is_null() -> None:
    assert _lockable_notional(None, _netted(_P_BOOK, "polymarket")) == (
        None, _NOTIONAL_BASIS_UNVERIFIED)


# ---------------------------------------------------------------------------
# 3-4. find_divergences: row fields, demotion ordering, include_warned
# ---------------------------------------------------------------------------


async def test_clean_row_carries_empty_warnings_and_notional(monkeypatch) -> None:
    _patch_pairs(monkeypatch, [_pair(k_age_s=60.0, p_age_s=45.0)])
    out = await find_divergences(min_net_edge=-1.0, limit=10, include_rules=False)
    d = out["divergences"][0]
    assert d["warnings"] == []
    assert d["max_lockable_notional"] == 46.0
    assert d["notional_basis"] == _NOTIONAL_BASIS_DEPTH
    assert out["warned_filtered"] == 0


async def test_near_resolution_and_stale_quote_warned(monkeypatch) -> None:
    # the probe signature: 0.14d-to-resolution legs + a >1h frozen quote.
    _patch_pairs(monkeypatch, [_pair(k_days=0.14, p_days=0.14, k_age_s=7200.0)])
    out = await find_divergences(min_net_edge=-1.0, limit=10, include_rules=False)
    d = out["divergences"][0]
    assert "near_resolution" in d["warnings"]
    assert "stale_quote" in d["warnings"]


async def test_resolution_mismatch_rides_warnings(monkeypatch) -> None:
    _patch_pairs(monkeypatch, [_pair(k_days=14.0, p_days=5.0)])
    out = await find_divergences(min_net_edge=-1.0, limit=10, include_rules=False)
    d = out["divergences"][0]
    assert d["resolution_mismatch"] is True  # existing field unchanged
    assert "resolution_mismatch" in d["warnings"]


async def test_depth_unverified_null_notional_plus_warning(monkeypatch) -> None:
    _patch_pairs(monkeypatch, [_pair(k_book={"bid": 0.54, "ask": 0.56})])
    out = await find_divergences(min_net_edge=-1.0, limit=10, include_rules=False)
    d = out["divergences"][0]
    assert d["max_lockable_notional"] is None
    assert d["notional_basis"] == _NOTIONAL_BASIS_UNVERIFIED
    assert "depth_unverified" in d["warnings"]


async def test_warned_rows_sort_after_clean_even_with_bigger_edge(monkeypatch) -> None:
    """The honesty fix: a warned row must sort AFTER every clean row even when its
    edge is larger (the buried-warning bug). Warned pair listed FIRST in the feed
    and with a wider gap (bigger edge + shorter lock -> bigger annualized edge)."""
    warned = _pair(k_days=0.5, p_days=0.5,
                   p_book={"bid": 0.14, "ask": 0.16, "bid_size": 300.0, "ask_size": 400.0})
    warned["a"]["id"], warned["b"]["id"] = "kalshi:WARNED", "polymarket:9"
    clean = _pair(k_age_s=60.0, p_age_s=45.0)
    _patch_pairs(monkeypatch, [warned, clean])
    out = await find_divergences(min_net_edge=-1.0, limit=10, include_rules=False)
    rows = out["divergences"]
    assert len(rows) == 2
    assert rows[0]["warnings"] == []                       # clean first
    assert "near_resolution" in rows[1]["warnings"]        # warned demoted
    # the demotion happened despite the warned row's bigger edge:
    assert rows[1]["net_edge"] > rows[0]["net_edge"]
    assert "clean-first" in out["ranked_by"]


async def test_equal_edge_warned_still_after_clean(monkeypatch) -> None:
    """At EQUAL edge, warned rows sort after clean (warned listed first upstream)."""
    warned = _pair(k_days=0.14, p_days=0.14)
    warned["a"]["id"], warned["b"]["id"] = "kalshi:WARNED2", "polymarket:8"
    clean = _pair()
    _patch_pairs(monkeypatch, [warned, clean])
    out = await find_divergences(min_net_edge=-1.0, limit=10, include_rules=False)
    rows = out["divergences"]
    assert len(rows) == 2
    assert rows[0]["warnings"] == []
    assert rows[1]["warnings"] != []


async def test_include_warned_false_filters_and_counts(monkeypatch) -> None:
    warned = _pair(k_days=0.14, p_days=0.14)
    warned["a"]["id"], warned["b"]["id"] = "kalshi:WARNED3", "polymarket:7"
    clean = _pair()
    _patch_pairs(monkeypatch, [warned, clean])
    out = await find_divergences(min_net_edge=-1.0, limit=10, include_rules=False,
                                 include_warned=False)
    rows = out["divergences"]
    assert len(rows) == 1
    assert rows[0]["warnings"] == []
    assert out["warned_filtered"] == 1


async def test_clean_rows_keep_edge_order_within_group(monkeypatch) -> None:
    """Within the clean group the existing (annualized-)edge ordering is intact."""
    small = _pair(p_book={"bid": 0.44, "ask": 0.46, "bid_size": 300.0, "ask_size": 400.0})
    small["a"]["id"], small["b"]["id"] = "kalshi:SMALL", "polymarket:6"
    big = _pair()  # ~30pt gap > ~10pt gap
    _patch_pairs(monkeypatch, [small, big])
    out = await find_divergences(min_net_edge=-1.0, limit=10, include_rules=False)
    rows = out["divergences"]
    assert len(rows) == 2
    assert rows[0]["net_edge"] > rows[1]["net_edge"]


# ---------------------------------------------------------------------------
# 5. MCP wrapper passthrough
# ---------------------------------------------------------------------------


async def test_t_find_divergences_forwards_include_warned(monkeypatch) -> None:
    captured: dict = {}

    async def _fake_find_divergences(**kw):  # noqa: ANN003
        captured.update(kw)
        return {"divergences": []}

    monkeypatch.setattr(server, "find_divergences", _fake_find_divergences)
    env = await server.t_find_divergences(min_net_edge=0.02, limit=3, include_warned=False)
    assert env["command"] == "t_find_divergences"
    assert captured["include_warned"] is False
    assert captured["min_net_edge"] == 0.02
    assert captured["limit"] == 3


async def test_t_find_divergences_default_include_warned_true(monkeypatch) -> None:
    captured: dict = {}

    async def _fake_find_divergences(**kw):  # noqa: ANN003
        captured.update(kw)
        return {"divergences": []}

    monkeypatch.setattr(server, "find_divergences", _fake_find_divergences)
    await server.t_find_divergences()
    assert captured["include_warned"] is True
