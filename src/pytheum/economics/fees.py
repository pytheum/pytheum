"""
Doc-verified taker-fee models for Kalshi + Polymarket, and the fee-netted cross-venue
edge. This is READ-SIDE DATA ANALYTICS — "after each venue's fees, the realizable edge on
this verified pair is X" — not order placement, sizing, or strategy.

Fee formulas are current as of 2026-06-29 and cross-checked against official docs; the full
table, sources, and confidence labels live in the matcher repo at
docs/research/2026-06-29-prediction-market-fees-and-settlement.md. Re-verify before any
money-moving use. Update the constants below when the venues change their schedules.

Both venues use the same SHAPE: fee = rate * contracts * price * (1 - price), peaking at
price=0.50 and → 0 at the extremes. Kalshi rounds UP to the cent and charges takers (maker
= 25% of taker); Polymarket charges takers only (makers 0%), category-dependent rate.
"""
from __future__ import annotations

import math
from typing import Optional

# --- Kalshi (taker, general tier). ceil(0.07 * C * P * (1-P)) to the cent. ---
KALSHI_GENERAL_COEFF = 0.07
KALSHI_INDEX_COEFF = 0.035          # S&P-500 (INX) / Nasdaq-100 markets (half tier)
KALSHI_MAKER_FRACTION = 0.25        # maker fee ≈ 25% of taker

# --- Polymarket taker fee rates by category (makers 0%). ---
# VERIFIED-from-docs (docs.polymarket.com/trading/fees, 2026 rollout). PM became non-zero
# in 2026 (crypto Jan / sports Feb / +8 categories Mar).
PM_FEE_RATES: dict[str, float] = {
    "crypto": 0.07,
    "economics": 0.05,
    "culture": 0.05,
    "weather": 0.05,
    "other": 0.05,
    "finance": 0.04,
    "politics": 0.04,
    "tech": 0.04,
    "mentions": 0.04,
    "sports": 0.03,
    "geopolitics": 0.0,
    "world": 0.0,
}
PM_DEFAULT_FEE_RATE = 0.05  # conservative middle when category is unknown

# Map the matcher's bet_type → a Polymarket fee category (for the divergence net-edge).
_SPORTS_BET_TYPES = frozenset({
    "moneyline", "moneyline_outcome", "moneyline_1h", "total", "total_1h", "total_2h",
    "total_f5", "spread", "spread_1h", "btts", "nrfi", "team_total", "team_corners",
    "halftime", "extra_innings", "tennis_ml", "tennis_set1", "tennis_total",
    "tennis_set_total", "esports_map", "esports_series", "esports_total", "prop",
    "player_prop", "nfl_prop", "wc_prop", "mlb_prop", "goalscorer", "pga_top",
    "ufc_ml", "ufc_distance", "winter_olympics_gold", "division_winner", "win_total",
})


def pm_fee_rate_for_bet_type(bet_type: Optional[str]) -> float:
    """Map a matcher bet_type to a Polymarket taker fee rate."""
    bt = (bet_type or "").strip().lower()
    if bt in _SPORTS_BET_TYPES:
        return PM_FEE_RATES["sports"]
    if "crypto" in bt:
        return PM_FEE_RATES["crypto"]
    if bt in ("event", "house_party"):
        return PM_FEE_RATES["politics"]
    return PM_DEFAULT_FEE_RATE


def _clamp_price(price: float) -> Optional[float]:
    """Prices outside (0,1) carry no fee curve (no valid contract)."""
    if price is None:
        return None
    p = float(price)
    if p <= 0.0 or p >= 1.0:
        return None
    return p


def kalshi_taker_fee(price: float, contracts: float = 1.0, *, coefficient: float = KALSHI_GENERAL_COEFF) -> float:
    """Kalshi taker fee in dollars: ceil(coeff * C * P * (1-P)) rounded UP to the cent."""
    p = _clamp_price(price)
    if p is None or contracts <= 0:
        return 0.0
    raw = coefficient * contracts * p * (1.0 - p)
    # Round UP to the next cent — but quantize the cents to 9dp first so float noise
    # (e.g. 1.75 -> 1.7500000000000002) doesn't spuriously bump an exact cent up.
    return math.ceil(round(raw * 100.0, 9)) / 100.0


def pm_taker_fee(price: float, contracts: float = 1.0, *, category: Optional[str] = None,
                 fee_rate: Optional[float] = None) -> float:
    """Polymarket taker fee in USDC: rate * C * P * (1-P). Makers are 0% (not modeled here)."""
    p = _clamp_price(price)
    if p is None or contracts <= 0:
        return 0.0
    rate = fee_rate if fee_rate is not None else PM_FEE_RATES.get((category or "").lower(), PM_DEFAULT_FEE_RATE)
    return rate * contracts * p * (1.0 - p)


def net_edge_after_fees(
    kalshi_implied: float,
    pm_implied: float,
    *,
    bet_type: Optional[str] = None,
    contracts: float = 1.0,
) -> Optional[dict]:
    """
    Fee-netted cross-venue edge for ONE matched pair, oriented to the Kalshi-YES frame.

    The arb that captures the divergence: buy the cheaper YES-equivalent and the opposite
    leg on the other venue (→ guaranteed $1 payout). Gross edge per $1 = |kalshi - pm|;
    you pay a taker fee on BOTH legs. Both fee curves are symmetric in P↔(1-P), so the
    fees can be evaluated at the implied prices directly.

    Returns a dict {gross_edge, net_edge, kalshi_fee, pm_fee, pm_fee_rate} in dollars per
    contract, or None if either implied is missing/degenerate.

    NOTE: net_edge is only a *risk-free* edge when the pair is settlement-fungible
    (arbitrage_clean). For correlated / timing_divergent pairs it is an APPARENT edge that
    carries basis/timing risk — surface the pair's `fungibility` alongside it.
    """
    pk = _clamp_price(kalshi_implied)
    pp = _clamp_price(pm_implied)
    if pk is None or pp is None:
        return None
    gross = abs(pk - pp) * contracts
    k_fee = kalshi_taker_fee(pk, contracts)
    rate = pm_fee_rate_for_bet_type(bet_type)
    p_fee = pm_taker_fee(pp, contracts, fee_rate=rate)
    return {
        "gross_edge": round(gross, 4),
        "net_edge": round(gross - k_fee - p_fee, 4),
        "kalshi_fee": round(k_fee, 4),
        "pm_fee": round(p_fee, 4),
        "pm_fee_rate": rate,
    }
