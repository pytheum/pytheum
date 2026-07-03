"""find_divergences page-local live-depth overlay (include_depth).

The equivalents-route books carry bid/ask but NO sizes, so pre-overlay every row
ships max_lockable_notional=null + depth_unverified. The overlay fetches both
legs' LIVE top-of-book (/v1/markets/{ref}/book?depth=1, coalesced server-side)
for the post-sort, post-limit page only. Covered:

1. Sizes arrive on both legs -> notional computed, live-depth basis, the
   depth_unverified warning removed, depth_overlaid counted.
2. Page re-sorted after overlays — a row that became clean floats above warned
   rows within the page.
3. One leg fails -> that row untouched (honest null + warning intact).
4. include_depth=False -> ZERO book fetches.
5. Overall-timeout path safe — page ships as-is.
6. Side-1-mapped poly leg: live sizes are swapped onto the complemented row book.
7. MCP wrapper — t_find_divergences forwards include_depth.
"""

from __future__ import annotations

import asyncio
import re
from typing import Any
from urllib.parse import unquote

import pytest

import pytheum.mcp.server as server
import pytheum.mcp.tools as tools
from pytheum.mcp.tools import (
    _NOTIONAL_BASIS_LIVE_DEPTH,
    _NOTIONAL_BASIS_UNVERIFIED,
    find_divergences,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# The REAL equivalents surface: two-sided books with bid/ask but NO sizes, so
# every row is depth_unverified until the live overlay sizes it.
_K_BOOK = {"bid": 0.54, "ask": 0.56}
_P_BOOK = {"bid": 0.24, "ask": 0.26}


def _pair(*, k_id: str = "kalshi:KXW", p_id: str = "polymarket:1",
          k_book: dict[str, Any] | None = None,
          p_book: dict[str, Any] | None = None) -> dict[str, Any]:
    """Synthetic /v1/markets/equivalents pair shaped like the live endpoint's
    (fresh legs, 5d horizon -> the only possible warning is depth_unverified)."""
    a = {"id": k_id, "venue": "kalshi", "question": "Will X happen?",
         "days_to_resolution": 5.0, "last_move_age_s": 60.0,
         "book": dict(k_book or _K_BOOK)}
    b = {"id": p_id, "venue": "polymarket", "question": "Will X happen?",
         "days_to_resolution": 5.0, "last_move_age_s": 45.0,
         "book": dict(p_book or _P_BOOK)}
    return {"bet_type": "event", "poly_side": None, "method": "opus_backstop",
            "confidence": 1.0, "a": a, "b": b}


def _book_resp(ref: str, *, bid: float, ask: float,
               bid_size: float | None, ask_size: float | None) -> dict[str, Any]:
    """Shape of the live GET /v1/markets/{ref}/book route (markets_book.py +
    trader.normalizers): bids/asks level arrays + a `top` summary carrying
    bid/bid_size/ask/ask_size."""
    return {
        "bids": [[bid, bid_size]], "asks": [[ask, ask_size]],
        "venue": ref.partition(":")[0], "ref": ref, "ts": "2026-07-02T00:00:00Z",
        "source": "live",
        "top": {"bid": bid, "bid_size": bid_size, "ask": ask, "ask_size": ask_size,
                "spread": round(ask - bid, 6), "mid": round((bid + ask) / 2, 6),
                "mid_reliable": True},
    }


_BOOK_PATH_RE = re.compile(r"/v1/markets/(.+)/book$")


def _patch_http(monkeypatch: pytest.MonkeyPatch, pairs: list[dict[str, Any]],
                books: dict[str, Any],
                calls: list[tuple[str, dict[str, Any]]] | None = None) -> None:
    """Patch tools._get: equivalents passes serve `pairs` (dedicated bet_type
    passes empty), /book paths serve `books[ref]` (an Exception value raises,
    an async callable is awaited — the timeout seam)."""

    async def _fake_get(path: str, params: dict[str, Any], base_url: str) -> dict[str, Any]:
        if calls is not None:
            calls.append((path, dict(params)))
        if path == "/v1/markets/equivalents":
            return {"pairs": pairs} if not params.get("bet_type") else {"pairs": []}
        m = _BOOK_PATH_RE.fullmatch(path)
        assert m, f"unexpected path {path!r}"
        assert params == {"depth": 1}, params  # top-of-book only, by design
        r = books[unquote(m.group(1))]
        if isinstance(r, Exception):
            raise r
        if callable(r):
            resp: dict[str, Any] = await r()
            return resp
        return dict(r)

    monkeypatch.setattr(tools, "_get", _fake_get)


def _book_calls(calls: list[tuple[str, dict[str, Any]]]) -> list[tuple[str, dict[str, Any]]]:
    return [c for c in calls if _BOOK_PATH_RE.fullmatch(c[0])]


# ---------------------------------------------------------------------------
# 1. Sizes arrive -> notional computed + warning cleared
# ---------------------------------------------------------------------------


async def test_live_sizes_populate_notional_and_clear_warning(monkeypatch: pytest.MonkeyPatch) -> None:
    """Both legs size at the live top -> same arithmetic as the stored-size case
    (PM YES leg 400 x 0.26 = 104; Kalshi NO leg 100 x (1-0.54) = 46 -> min 46),
    with the live-depth basis and NO depth_unverified warning."""
    _patch_http(monkeypatch, [_pair()], {
        "kalshi:KXW": _book_resp("kalshi:KXW", bid=0.54, ask=0.56,
                                 bid_size=100.0, ask_size=200.0),
        "polymarket:1": _book_resp("polymarket:1", bid=0.24, ask=0.26,
                                   bid_size=300.0, ask_size=400.0),
    })
    out = await find_divergences(min_net_edge=-1.0, limit=10, include_rules=False)
    d = out["divergences"][0]
    assert d["max_lockable_notional"] == 46.0
    assert d["notional_basis"] == _NOTIONAL_BASIS_LIVE_DEPTH
    assert "depth_unverified" not in d["warnings"]
    assert d["warnings"] == []
    # the live sizes are surfaced on the row's books (prices unchanged)
    assert d["a"]["book"]["bid_size"] == 100.0 and d["a"]["book"]["ask_size"] == 200.0
    assert d["b"]["book"]["bid_size"] == 300.0 and d["b"]["book"]["ask_size"] == 400.0
    assert d["a"]["book"]["bid"] == 0.54 and d["b"]["book"]["ask"] == 0.26
    assert out["depth_overlaid"] == 1
    assert "_poly_flipped" not in d  # internal flag never leaks


# ---------------------------------------------------------------------------
# 2. Page re-sorted after overlays
# ---------------------------------------------------------------------------


async def test_row_that_became_clean_floats_above_warned_within_page(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pre-overlay BOTH rows are warned (depth_unverified) and the bigger-edge row
    ranks first within the warned group. The overlay sizes only the SMALLER-edge
    row -> it becomes clean and must float above the still-warned bigger edge."""
    big = _pair(k_id="kalshi:BIG", p_id="polymarket:9",
                p_book={"bid": 0.14, "ask": 0.16})   # ~40pt gap, never sized
    small = _pair(k_id="kalshi:SMALL", p_id="polymarket:6")  # ~30pt gap, sized live
    _patch_http(monkeypatch, [big, small], {
        "kalshi:BIG": RuntimeError("venue down"),
        "polymarket:9": _book_resp("polymarket:9", bid=0.14, ask=0.16,
                                   bid_size=50.0, ask_size=50.0),
        "kalshi:SMALL": _book_resp("kalshi:SMALL", bid=0.54, ask=0.56,
                                   bid_size=100.0, ask_size=200.0),
        "polymarket:6": _book_resp("polymarket:6", bid=0.24, ask=0.26,
                                   bid_size=300.0, ask_size=400.0),
    })
    out = await find_divergences(min_net_edge=-1.0, limit=10, include_rules=False)
    rows = out["divergences"]
    assert len(rows) == 2
    assert rows[0]["a"]["market_id"] == "kalshi:SMALL"    # became clean -> first
    assert rows[0]["warnings"] == []
    assert rows[1]["a"]["market_id"] == "kalshi:BIG"      # still warned -> demoted
    assert "depth_unverified" in rows[1]["warnings"]
    assert rows[1]["net_edge"] > rows[0]["net_edge"]      # despite the bigger edge
    assert out["depth_overlaid"] == 1


# ---------------------------------------------------------------------------
# 3. Per-leg failure -> honest null, row untouched
# ---------------------------------------------------------------------------


async def test_one_leg_failure_leaves_row_untouched(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_http(monkeypatch, [_pair()], {
        "kalshi:KXW": RuntimeError("kalshi 502"),
        "polymarket:1": _book_resp("polymarket:1", bid=0.24, ask=0.26,
                                   bid_size=300.0, ask_size=400.0),
    })
    out = await find_divergences(min_net_edge=-1.0, limit=10, include_rules=False)
    d = out["divergences"][0]
    assert d["max_lockable_notional"] is None
    assert d["notional_basis"] == _NOTIONAL_BASIS_UNVERIFIED
    assert "depth_unverified" in d["warnings"]
    assert out["depth_overlaid"] == 0


async def test_degraded_book_response_leaves_row_untouched(monkeypatch: pytest.MonkeyPatch) -> None:
    """The book route never 500s — venue errors come back 200 with an `error` key
    and no `top`. That's a failed leg, not sizes."""
    _patch_http(monkeypatch, [_pair()], {
        "kalshi:KXW": {"error": "venue_unavailable", "detail": "boom",
                       "source": "unavailable", "venue": "kalshi", "ref": "kalshi:KXW"},
        "polymarket:1": _book_resp("polymarket:1", bid=0.24, ask=0.26,
                                   bid_size=300.0, ask_size=400.0),
    })
    out = await find_divergences(min_net_edge=-1.0, limit=10, include_rules=False)
    d = out["divergences"][0]
    assert d["max_lockable_notional"] is None
    assert "depth_unverified" in d["warnings"]
    assert out["depth_overlaid"] == 0


async def test_live_top_missing_size_leaves_row_untouched(monkeypatch: pytest.MonkeyPatch) -> None:
    """A live book with an empty side (top.ask_size null) can't verify depth."""
    _patch_http(monkeypatch, [_pair()], {
        "kalshi:KXW": _book_resp("kalshi:KXW", bid=0.54, ask=0.56,
                                 bid_size=100.0, ask_size=None),
        "polymarket:1": _book_resp("polymarket:1", bid=0.24, ask=0.26,
                                   bid_size=300.0, ask_size=400.0),
    })
    out = await find_divergences(min_net_edge=-1.0, limit=10, include_rules=False)
    d = out["divergences"][0]
    assert d["max_lockable_notional"] is None
    assert "depth_unverified" in d["warnings"]


# ---------------------------------------------------------------------------
# 4. include_depth=False -> zero book fetches
# ---------------------------------------------------------------------------


async def test_include_depth_false_makes_zero_book_fetches(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, dict[str, Any]]] = []
    _patch_http(monkeypatch, [_pair()], {}, calls=calls)  # any book fetch would KeyError
    out = await find_divergences(min_net_edge=-1.0, limit=10, include_rules=False,
                                 include_depth=False)
    assert _book_calls(calls) == []
    d = out["divergences"][0]
    assert d["max_lockable_notional"] is None
    assert "depth_unverified" in d["warnings"]
    assert out["depth_overlaid"] == 0
    assert "_poly_flipped" not in d


async def test_overlay_is_page_local_rows_beyond_limit_not_fetched(monkeypatch: pytest.MonkeyPatch) -> None:
    """Bounded cost <= 2 x limit: only the returned page's legs are fetched."""
    p1 = _pair(k_id="kalshi:P1", p_id="polymarket:11",
               p_book={"bid": 0.14, "ask": 0.16})  # bigger edge -> the page
    p2 = _pair(k_id="kalshi:P2", p_id="polymarket:12")
    calls: list[tuple[str, dict[str, Any]]] = []
    _patch_http(monkeypatch, [p1, p2], {
        "kalshi:P1": _book_resp("kalshi:P1", bid=0.54, ask=0.56,
                                bid_size=100.0, ask_size=200.0),
        "polymarket:11": _book_resp("polymarket:11", bid=0.14, ask=0.16,
                                    bid_size=300.0, ask_size=400.0),
    }, calls=calls)
    out = await find_divergences(min_net_edge=-1.0, limit=1, include_rules=False)
    assert len(out["divergences"]) == 1
    assert len(_book_calls(calls)) == 2  # the page's two legs only, never p2's


# ---------------------------------------------------------------------------
# 5. Overall timeout -> page ships as-is
# ---------------------------------------------------------------------------


async def test_overall_timeout_leaves_page_untouched(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _hang() -> dict[str, Any]:
        await asyncio.sleep(0.5)
        return _book_resp("kalshi:KXW", bid=0.54, ask=0.56,
                          bid_size=100.0, ask_size=200.0)

    monkeypatch.setattr(tools, "_DEPTH_OVERLAY_TIMEOUT_S", 0.05)
    _patch_http(monkeypatch, [_pair()], {"kalshi:KXW": _hang, "polymarket:1": _hang})
    out = await find_divergences(min_net_edge=-1.0, limit=10, include_rules=False)
    d = out["divergences"][0]
    assert d["max_lockable_notional"] is None
    assert d["notional_basis"] == _NOTIONAL_BASIS_UNVERIFIED
    assert "depth_unverified" in d["warnings"]
    assert out["depth_overlaid"] == 0


# ---------------------------------------------------------------------------
# 6. Side-1-mapped poly leg: live sizes swap onto the complemented book
# ---------------------------------------------------------------------------


async def test_side_mapped_flip_swaps_live_sizes(monkeypatch: pytest.MonkeyPatch) -> None:
    """A poly_side=1 pair's row book is the COMPLEMENT of the live poly book
    (_orient_poly_leg: bid'=1-ask, sizes swap), so the overlay must swap the live
    sizes too: the live BID (0.60 x 400) becomes the oriented ASK (0.40 x 400)
    the YES buy hits; the live ASK's 300 backs the oriented BID. Un-swapped, the
    notional would read the wrong side's 300 x 0.40 = 120; swapped it is
    400 x 0.40 = 160 (the poly YES leg binds vs the huge Kalshi NO leg)."""
    pair: dict[str, Any] = {
        "bet_type": "moneyline", "poly_side": 1, "poly_outcome": "Team B",
        "method": "game_title_match", "confidence": None,
        "a": {"id": "kalshi:KXML", "venue": "kalshi", "question": "Will B beat A?",
              "days_to_resolution": 5.0, "last_move_age_s": 60.0,
              "book": {"bid": 0.54, "ask": 0.56}},
        # RAW poly book (first-listed outcome = Team A): oriented -> bid 0.36 / ask 0.40
        "b": {"id": "polymarket:77", "venue": "polymarket", "question": "A vs B",
              "implied_yes": 0.62, "days_to_resolution": 5.0, "last_move_age_s": 45.0,
              "book": {"bid": 0.60, "ask": 0.64}},
    }
    _patch_http(monkeypatch, [pair], {
        # Kalshi sizes huge so the poly YES leg is binding either way.
        "kalshi:KXML": _book_resp("kalshi:KXML", bid=0.54, ask=0.56,
                                  bid_size=10000.0, ask_size=10000.0),
        # Live poly book is the RAW side: bid 0.60 x 400, ask 0.64 x 300.
        "polymarket:77": _book_resp("polymarket:77", bid=0.60, ask=0.64,
                                    bid_size=400.0, ask_size=300.0),
    })
    out = await find_divergences(min_net_edge=-1.0, limit=10, include_rules=False)
    d = out["divergences"][0]
    # Direction: buy YES on the oriented poly ask (0.40). The oriented ask is the
    # complement of the live BID (0.60), so its size is the live bid's 400.
    assert d["max_lockable_notional"] == 160.0
    assert d["b"]["book"]["ask_size"] == 400.0 and d["b"]["book"]["bid_size"] == 300.0
    assert d["notional_basis"] == _NOTIONAL_BASIS_LIVE_DEPTH
    assert "depth_unverified" not in d["warnings"]


# ---------------------------------------------------------------------------
# 7. MCP wrapper passthrough
# ---------------------------------------------------------------------------


async def test_t_find_divergences_forwards_include_depth(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    async def _fake_find_divergences(**kw: Any) -> dict[str, Any]:
        captured.update(kw)
        return {"divergences": []}

    monkeypatch.setattr(server, "find_divergences", _fake_find_divergences)
    env = await server.t_find_divergences(include_depth=False)
    assert env["command"] == "t_find_divergences"
    assert captured["include_depth"] is False


async def test_t_find_divergences_default_include_depth_true(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    async def _fake_find_divergences(**kw: Any) -> dict[str, Any]:
        captured.update(kw)
        return {"divergences": []}

    monkeypatch.setattr(server, "find_divergences", _fake_find_divergences)
    await server.t_find_divergences()
    assert captured["include_depth"] is True
