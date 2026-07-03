"""Executable, fee-netted edge on /v1/markets/matched (the honest arb radar).

Regression for the eval finding: sort_by=spread surfaced mid-spread phantoms
(a wide one-sided book shows a big |mid diff| that dies on execution). The
`net_edge` field + sort rank by the real locked-arb edge after Kalshi fees.
"""

from __future__ import annotations

from pytheum.api.markets_matched import _build_sort_key, _cross_venue
from pytheum.api.params import kalshi_fee, locked_arb_net_edge


def test_kalshi_fee_curve() -> None:
    assert kalshi_fee(0.5) == 0.0175  # 0.07 * 0.5 * 0.5
    assert kalshi_fee(0.0) == 0.0
    assert kalshi_fee(1.0) == 0.0


def test_phantom_wide_book_is_negative() -> None:
    # NY-18-like: ~30% mid "spread" but a near-empty Kalshi book → no real arb.
    net = locked_arb_net_edge({"bid": 0.004, "ask": 0.99}, {"bid": 0.369, "ask": 0.874})
    assert net is not None and net < 0


def test_real_gap_tight_books_is_positive() -> None:
    # Kalshi YES genuinely cheap (0.42) vs PM YES dear (0.62), both tight.
    net = locked_arb_net_edge({"bid": 0.40, "ask": 0.42}, {"bid": 0.60, "ask": 0.62})
    assert net is not None and net > 0.10


def test_none_when_one_sided() -> None:
    assert locked_arb_net_edge({"bid": 0.4}, {"bid": 0.6, "ask": 0.62}) is None
    assert locked_arb_net_edge(None, {"bid": 0.6, "ask": 0.62}) is None


def test_cross_venue_emits_net_edge_and_keeps_mid() -> None:
    cv = _cross_venue(
        {"implied_yes": 0.50, "book": {"bid": 0.40, "ask": 0.42}},
        {"implied_yes": 0.61, "book": {"bid": 0.60, "ask": 0.62}},
    )
    assert cv["executable"] is True
    assert cv["net_edge"] > 0.10
    assert "spread" in cv  # mid retained for back-compat


def test_net_edge_sort_ranks_real_first_unpriced_last() -> None:
    key = _build_sort_key("net_edge")
    real = {"cross_venue": {"net_edge": 0.05}}
    phantom = {"cross_venue": {"net_edge": -0.60}}
    unpriced = {"cross_venue": {}}
    ranked = sorted([unpriced, phantom, real], key=key, reverse=True)
    assert ranked[0] is real
    assert ranked[-1] is unpriced


async def test_matched_inflight_cap_sheds_load() -> None:
    """Beyond _MATCHED_MAX_INFLIGHT concurrent requests → clean 429 (not a pile-up)."""
    from pytheum.api import markets_matched as mm
    old = mm._MATCHED_MAX_INFLIGHT
    mm._MATCHED_MAX_INFLIGHT = 0  # saturate immediately
    try:
        status, body = await mm.handle_markets_matched({}, dao=None, equivalence=None)
        assert status == 429
        assert body["error"] == "rate_limited" and body["retry_after"] == 1
    finally:
        mm._MATCHED_MAX_INFLIGHT = old
