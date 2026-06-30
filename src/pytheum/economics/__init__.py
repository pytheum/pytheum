"""Read-side trading-economics helpers (fee models, net-edge). Data analytics on
verified pairs — NOT order placement, sizing, or strategy."""
from pytheum.economics.fees import (
    kalshi_taker_fee,
    pm_taker_fee,
    pm_fee_rate_for_bet_type,
    net_edge_after_fees,
    KALSHI_GENERAL_COEFF,
    PM_FEE_RATES,
)

__all__ = [
    "kalshi_taker_fee",
    "pm_taker_fee",
    "pm_fee_rate_for_bet_type",
    "net_edge_after_fees",
    "KALSHI_GENERAL_COEFF",
    "PM_FEE_RATES",
]
