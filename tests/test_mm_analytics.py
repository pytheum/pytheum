"""Unit tests for the MM analytics layer (pytheum.mm.reference + resolution_fields)."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

from pytheum.mm import (
    Leg,
    advise,
    as_reference_quote,
    divergence_from_text,
    extract_fields,
    fungibility,
    reference_fair_value,
    settlement_divergence,
    terminal_variance,
    time_to_resolution_years,
)


def _approx(a, b, tol=1e-6):
    return abs(a - b) <= tol


# ---- reference fair value -------------------------------------------------
def test_reference_weights_tighter_deeper_leg():
    k = Leg("kalshi", bid=0.54, ask=0.56, bid_size=100, ask_size=100)     # tight+deep, mid .55
    p = Leg("polymarket", bid=0.55, ask=0.65, bid_size=10, ask_size=10)   # wide+thin, mid .60
    p_hat, basis = reference_fair_value(k, p)
    assert p_hat < 0.56, f"p_hat should be pulled toward the tight leg, got {p_hat}"
    assert _approx(basis, 0.55 - 0.60)                                     # kalshi_mid - pm_mid


def test_reference_single_leg_fallback():
    k = Leg("kalshi", bid=0.40, ask=0.42, bid_size=50, ask_size=50)
    p = Leg("polymarket")                                                  # no price
    p_hat, basis = reference_fair_value(k, p)
    assert _approx(p_hat, 0.41) and basis is None                          # falls back to the live leg


# ---- fungibility ----------------------------------------------------------
def test_fungibility_deterministic_is_fungible():
    assert fungibility("structured_key", None).fungible is True
    assert fungibility("game_title_match", 0.5).fungible is True           # deterministic wins over low conf


def test_fungibility_judged_needs_confidence_floor():
    assert fungibility("opus_backstop", 0.95).fungible is True
    assert fungibility("opus_backstop", 0.70).fungible is False            # below floor -> confirm rules


def test_fungibility_settlement_divergence_vetoes():
    v = fungibility("structured_key", 1.0, settlement_divergence=True)
    assert v.fungible is False and "resolve differently" in v.reason       # even a deterministic match


# ---- risk inputs ----------------------------------------------------------
def test_terminal_variance_is_bernoulli():
    assert _approx(terminal_variance(0.5), 0.25)                           # max at 0.5
    assert terminal_variance(0.99) < 0.02                                  # -> 0 near certainty
    assert terminal_variance(None) is None


def test_time_to_resolution_years():
    t = time_to_resolution_years((datetime.now(UTC) + timedelta(days=365)).isoformat())
    assert 0.99 < t < 1.01
    assert time_to_resolution_years(None) is None


# ---- A-S reference quote --------------------------------------------------
def test_as_quote_skews_against_inventory():
    long = as_reference_quote(0.5, inventory=+10, T_years=0.1, gamma=1.0, kappa=20.0)
    short = as_reference_quote(0.5, inventory=-10, T_years=0.1, gamma=1.0, kappa=20.0)
    assert long["inventory_skew"] < 0 < short["inventory_skew"]            # long -> skew down, short -> up
    assert _approx(long["reservation_price"], 0.5 - 10 * 1.0 * 0.25 * 0.1)  # r = p - q*gamma*p(1-p)*T


def test_as_quote_spread_widens_with_time_and_risk():
    near = as_reference_quote(0.5, 0, T_years=0.01, gamma=1.0, kappa=20.0)
    far = as_reference_quote(0.5, 0, T_years=1.0, gamma=1.0, kappa=20.0)
    assert far["half_spread"] > near["half_spread"]                        # more time-to-resolution -> wider


def test_as_quote_clips_to_unit_interval():
    q = as_reference_quote(0.97, inventory=-1000, T_years=1.0, gamma=5.0, kappa=20.0)  # huge upward skew
    assert 0.0 <= q["bid"] <= 1.0 and 0.0 <= q["ask"] <= 1.0               # a probability can't leave [0,1]


# ---- advise (full record + warnings) --------------------------------------
def test_advise_fungible_tight_pair_clean():
    k = Leg("kalshi", bid=0.54, ask=0.56, bid_size=500, ask_size=500)
    p = Leg("polymarket", bid=0.545, ask=0.555, bid_size=800, ask_size=800)
    out = advise(k, p, resolution_at=(datetime.now(UTC) + timedelta(days=30)).isoformat(),
                 method="structured_key", confidence=1.0)
    assert out["fungibility"]["fungible"] is True
    assert out["warnings"] == []                                           # tight, fungible, both legs live
    assert out["risk_inputs"]["terminal_variance"] is not None
    # the gamma/inventory-free A-S kernel = terminal_variance * T
    ri = out["risk_inputs"]
    assert _approx(ri["inventory_risk_gradient"],
                   ri["terminal_variance"] * ri["time_to_resolution_years"])


def test_advise_risk_gradient_none_without_resolution():
    """No resolution timestamp -> no horizon -> the A-S kernel is honestly null, not guessed."""
    k = Leg("kalshi", bid=0.54, ask=0.56, bid_size=500, ask_size=500)
    p = Leg("polymarket", bid=0.545, ask=0.555, bid_size=800, ask_size=800)
    out = advise(k, p, method="structured_key", confidence=1.0)            # resolution_at omitted
    assert out["risk_inputs"]["time_to_resolution_years"] is None
    assert out["risk_inputs"]["inventory_risk_gradient"] is None


def test_advise_flags_non_fungible_and_wide_basis():
    k = Leg("kalshi", bid=0.30, ask=0.32, bid_size=100, ask_size=100)      # mid .31
    p = Leg("polymarket", bid=0.55, ask=0.57, bid_size=100, ask_size=100)  # mid .56 -> basis -.25
    out = advise(k, p, method="opus_backstop", confidence=0.6)
    assert out["fungibility"]["fungible"] is False
    assert any("not_fungible" in w for w in out["warnings"])               # don't quote it as a hedge
    assert any("wide_basis" in w for w in out["warnings"])


def test_advise_rules_divergence_overrides_confident_match():
    """Even a deterministic, high-confidence match is marked NOT fungible when the resolution
    RULES diverge (different threshold) — the settlement-divergence detector is the hard gate."""
    k = Leg("kalshi", bid=0.48, ask=0.50, bid_size=300, ask_size=300)
    p = Leg("polymarket", bid=0.49, ask=0.51, bid_size=300, ask_size=300)
    out = advise(k, p, method="structured_key", confidence=1.0,
                 kalshi_rules="Resolves YES if CPI is above 3.0% per BLS.",
                 pm_rules="Resolves YES if CPI exceeds 3.25%.")
    assert out["fungibility"]["fungible"] is False                 # rules override the confident match
    assert any("settlement_divergence" in w and "threshold" in w for w in out["warnings"])


def test_advise_matching_rules_stays_fungible():
    k = Leg("kalshi", bid=0.60, ask=0.62, bid_size=500, ask_size=500)
    p = Leg("polymarket", bid=0.605, ask=0.615, bid_size=500, ask_size=500)
    out = advise(k, p, method="game_title_match", confidence=1.0,
                 kalshi_rules="Resolves to the winning team per the official final score.",
                 pm_rules="Resolves based on the official final result of the game.")
    assert out["fungibility"]["fungible"] is True and out["warnings"] == []


# ---- resolution fields (settlement-divergence detector) -------------------
_CPI_KALSHI = "This market resolves YES if headline CPI year-over-year is above 3.0% per the BLS release."
_CPI_PM = "Resolves YES if year-over-year CPI exceeds 3.25% as officially reported."
_GAME_KALSHI = "Resolves based on the official final score of the game."
_GAME_PM = "Resolves to the winning team per the official final result."
_AP_KALSHI = "Market resolves per the Associated Press race call."
_UMA_PM = "Resolves via the UMA optimistic oracle."


def test_extract_threshold_and_source():
    f = extract_fields(_CPI_KALSHI)
    assert 3.0 in f.thresholds
    assert "BLS" in f.sources
    assert f.has_rules


def test_threshold_mismatch_flags_divergence():
    div, reasons = divergence_from_text(_CPI_KALSHI, _CPI_PM)     # 3.0 vs 3.25
    assert div is True
    assert any("threshold mismatch" in r for r in reasons)


def test_matching_thresholds_no_divergence():
    div, _ = divergence_from_text(_CPI_KALSHI, _CPI_KALSHI)
    assert div is False                                            # same threshold + source


def test_source_mismatch_flags_divergence():
    div, reasons = divergence_from_text(_AP_KALSHI, _UMA_PM)       # AP vs UMA
    assert div is True and any("settlement source differs" in r for r in reasons)


def test_fungible_game_no_divergence():
    div, reasons = divergence_from_text(_GAME_KALSHI, _GAME_PM)    # no thresholds, no named-source conflict
    assert div is False and reasons == []


def test_missing_rules_never_false_flags():
    assert divergence_from_text(_CPI_KALSHI, None) == (False, [])  # can't compare -> don't flag
    assert divergence_from_text(None, None) == (False, [])


def test_disjoint_only_flags_when_both_present():
    f_a = extract_fields("resolves if above 50")
    f_b = extract_fields("resolves per the official result")
    assert settlement_divergence(f_a, f_b) == (False, [])          # under-flag by design
