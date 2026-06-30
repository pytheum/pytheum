"""Tests for the doc-verified fee model + fee-netted edge (pytheum.economics.fees)."""
import math

import pytest

from pytheum.economics.fees import (
    kalshi_taker_fee,
    pm_taker_fee,
    pm_fee_rate_for_bet_type,
    net_edge_after_fees,
    PM_FEE_RATES,
)


def test_kalshi_fee_peaks_at_half_and_rounds_up_to_cent():
    # 0.07 * 0.25 = 0.0175 -> ceil to the cent -> $0.02
    assert kalshi_taker_fee(0.5) == 0.02


def test_kalshi_fee_symmetric_in_price():
    assert kalshi_taker_fee(0.3) == kalshi_taker_fee(0.7)


def test_kalshi_fee_zero_at_or_beyond_extremes():
    assert kalshi_taker_fee(0.0) == 0.0
    assert kalshi_taker_fee(1.0) == 0.0
    assert kalshi_taker_fee(-0.1) == 0.0


def test_kalshi_fee_scales_with_contracts():
    # 0.07 * 100 * 0.25 = 1.75 -> ceil(175c)/100 = $1.75
    assert kalshi_taker_fee(0.5, contracts=100) == 1.75


def test_pm_fee_uses_category_rate_no_rounding():
    assert pm_taker_fee(0.5, category="crypto") == pytest.approx(0.07 * 0.25)
    assert pm_taker_fee(0.5, category="sports") == pytest.approx(0.03 * 0.25)


def test_pm_fee_geopolitics_is_free_and_makers_unmodeled():
    assert pm_taker_fee(0.5, category="geopolitics") == 0.0
    assert PM_FEE_RATES["geopolitics"] == 0.0


def test_pm_fee_rate_mapping_from_bet_type():
    assert pm_fee_rate_for_bet_type("moneyline") == 0.03   # sports
    assert pm_fee_rate_for_bet_type("event") == 0.04       # politics
    assert pm_fee_rate_for_bet_type("crypto_band") == 0.07
    assert pm_fee_rate_for_bet_type("unknown_type") == 0.05  # conservative default


def test_net_edge_after_fees_basic():
    # gross 0.10; kalshi_fee(0.60)=ceil(0.07*0.6*0.4*100)/100=0.02; pm sports fee(0.50)=0.03*0.25=0.0075
    r = net_edge_after_fees(0.60, 0.50, bet_type="moneyline")
    assert r["gross_edge"] == pytest.approx(0.10, abs=1e-6)
    assert r["kalshi_fee"] == 0.02
    assert r["pm_fee"] == pytest.approx(0.0075, abs=1e-6)
    assert r["net_edge"] == pytest.approx(0.10 - 0.02 - 0.0075, abs=1e-6)
    assert r["pm_fee_rate"] == 0.03


def test_net_edge_none_when_degenerate_or_missing():
    assert net_edge_after_fees(None, 0.5) is None
    assert net_edge_after_fees(0.5, 1.0) is None   # pm at extreme -> no contract
    assert net_edge_after_fees(0.0, 0.5) is None


def test_net_edge_can_go_negative_when_fees_exceed_gross():
    # Tiny gross divergence is eaten by fees.
    r = net_edge_after_fees(0.501, 0.500, bet_type="event")
    assert r["net_edge"] < 0
