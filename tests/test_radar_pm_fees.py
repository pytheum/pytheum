"""Locks the 2026 fix: Polymarket is NO LONGER 0% fee in either radar's fee model
(params.locked_arb_net_edge for /markets/matched, tools._fee_dollars for
t_find_divergences). Both source rates from pytheum.economics.fees."""
from pytheum.api.params import locked_arb_net_edge
from pytheum.mcp.tools import _fee_dollars, _row_fee_bps


def test_pm_taker_fee_now_nonzero_in_divergence_helpers():
    # The bug this fixes: PM used to hardcode 0. Now category-dependent + nonzero.
    assert _fee_dollars("polymarket", 0.5, bet_type="moneyline") > 0
    assert _row_fee_bps("polymarket", 0.5, bet_type="moneyline") > 0
    # Kalshi unchanged (still charged).
    assert _fee_dollars("kalshi", 0.5) > 0


def test_pm_fee_is_category_dependent_in_divergence_helpers():
    # crypto (0.07) costs more than sports (0.03) at the same price.
    assert _fee_dollars("polymarket", 0.5, bet_type="crypto_band") > \
        _fee_dollars("polymarket", 0.5, bet_type="moneyline")


def test_locked_arb_nets_pm_fee_and_is_category_dependent():
    k = {"bid": 0.40, "ask": 0.42}
    pm = {"bid": 0.60, "ask": 0.62}
    crypto = locked_arb_net_edge(k, pm, bet_type="crypto")     # PM rate 0.07
    sports = locked_arb_net_edge(k, pm, bet_type="moneyline")  # PM rate 0.03
    assert crypto is not None and sports is not None
    # Higher PM fee -> more cost -> lower net edge.
    assert crypto < sports


def test_locked_arb_pm_fee_reduces_edge_vs_unfeed_baseline():
    # Sanity: netting PM fee strictly lowers the edge below a no-PM-fee computation.
    k = {"bid": 0.40, "ask": 0.42}
    pm = {"bid": 0.60, "ask": 0.62}
    netted = locked_arb_net_edge(k, pm, bet_type="moneyline")
    # geopolitics maps through the default (0.05) here; the point is netted < gross.
    gross_mid = abs(0.41 - 0.61)  # ~mid spread, fee-free upper bound
    assert netted is not None and netted < gross_mid
