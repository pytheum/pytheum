"""Bug 2 regression — find_divergences reprices a STALE stored quote from the live book.

The phantom: the live-depth overlay fetched the poly leg's live top-of-book (sizes filled
→ warnings empty, notional_basis="live"), but KEPT the stale stored PRICES (only swapped
sizes) — so net_edge was computed off a lagged store quote (Banxico: net_edge 0.30 vs real
~0.01). Fix: the overlay compares live vs stored mid, and when they diverge > 5c reprices
from the live book, re-nets fees, warns stale_quote, and the row is demoted on re-sort.
"""
from __future__ import annotations

from typing import Any

import pytheum.mcp.tools as tools
from pytheum.mcp.tools import find_divergences
from tests.test_divergences_depth_overlay import _book_resp, _pair, _patch_http


async def test_stale_poly_quote_repriced_and_demoted(monkeypatch) -> None:
    """Banxico shape: stored poly book 0.24/0.26 (mid .25), but the LIVE poly CLOB is
    0.72/0.74 (mid .73) — a 0.48 divergence. The old overlay kept .24/.26 and showed a fat
    phantom edge; now the row reprices to the live book, warns stale_quote, recomputes a
    much smaller edge, and is demoted below clean rows."""
    # Kalshi leg fresh + matches live; poly leg's live quote has moved far from the store.
    _patch_http(monkeypatch, [_pair()], {
        "kalshi:KXW": _book_resp("kalshi:KXW", bid=0.54, ask=0.56, bid_size=100.0, ask_size=200.0),
        "polymarket:1": _book_resp("polymarket:1", bid=0.72, ask=0.74, bid_size=300.0, ask_size=400.0),
    })
    out = await find_divergences(min_net_edge=-1.0, limit=10, include_rules=False)
    d = out["divergences"][0]
    # repriced to the live book (not the stale 0.24/0.26)
    assert d["b"]["book"]["bid"] == 0.72 and d["b"]["book"]["ask"] == 0.74
    assert "repriced" in d["b"]["book"]["book_source"]
    assert d["a"]["book"]["book_source"] == "live"        # kalshi within 5c → not repriced
    assert "stale_quote" in d["warnings"]                  # the honesty flag now fires
    assert out["stale_repriced"] == 1
    # edge recomputed from live prices: kalshi YES ~0.56 + poly NO ~ (1-0.72)=0.28 ≈ 0.84 cost
    # → net_edge ≈ 0.16 and NOT the ~0.49 phantom the stale 0.26 poly ask implied.
    assert d["net_edge"] < 0.30


async def test_fresh_quote_not_repriced(monkeypatch) -> None:
    """Live mid within 5c of the stored mid → no reprice, no stale_quote, book_source live."""
    _patch_http(monkeypatch, [_pair()], {
        "kalshi:KXW": _book_resp("kalshi:KXW", bid=0.54, ask=0.56, bid_size=100.0, ask_size=200.0),
        "polymarket:1": _book_resp("polymarket:1", bid=0.25, ask=0.27, bid_size=300.0, ask_size=400.0),
    })
    out = await find_divergences(min_net_edge=-1.0, limit=10, include_rules=False)
    d = out["divergences"][0]
    assert d["b"]["book"]["bid"] == 0.24 and d["b"]["book"]["ask"] == 0.26   # stored prices kept
    assert d["b"]["book"]["book_source"] == "live"
    assert "stale_quote" not in d["warnings"]
    assert out["stale_repriced"] == 0


async def test_failed_leg_marked_book_source_store(monkeypatch) -> None:
    """A leg whose live fetch fails is labeled book_source='store' (unverifiable) and the
    row keeps its honest null + depth_unverified (demoted)."""
    _patch_http(monkeypatch, [_pair()], {
        "kalshi:KXW": {"error": "venue_unavailable", "source": "unavailable", "ref": "kalshi:KXW"},
        "polymarket:1": _book_resp("polymarket:1", bid=0.24, ask=0.26, bid_size=300.0, ask_size=400.0),
    })
    out = await find_divergences(min_net_edge=-1.0, limit=10, include_rules=False)
    d = out["divergences"][0]
    assert d["a"]["book"]["book_source"] == "store"        # failed fetch → unverifiable
    assert d["max_lockable_notional"] is None
    assert "depth_unverified" in d["warnings"]
    assert out["stale_repriced"] == 0
