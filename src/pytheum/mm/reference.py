"""MM reference layer — the cross-venue fair-value + fungibility signal a prediction-market
MARKET MAKER plugs into its OWN quoting model.

The premise: SIG/Jump-class MMs already own feeds, OMS, and quoting models better than anything
we'd build. What a raw single-venue feed CANNOT give them is (a) a CROSS-VENUE reference fair
value fusing both venues' prices into one probability, and (b) whether the two contracts are
actually FUNGIBLE — safe to treat one as a hedge/anchor for the other — which is the #1 MM risk
given the 2026 settlement-dispute crisis (1,150+ disputed Polymarket markets, the $60M UMA
dispute). Both require Pytheum's settlement-verified equivalence graph + resolution semantics:
the layer that's expensive to build, has no alpha in it, and is the canonical "buy, don't
build" line item.

This is the ANALYTICS layer, not a data pipe. Given a matched pair's two legs (price + book) +
resolution metadata + the matcher's method/confidence, it returns:
  - p_hat        : the cross-venue reference fair value (depth/tightness-weighted blend)
  - basis        : kalshi_implied - pm_implied (the cross-venue divergence)
  - fungibility  : is the pair safe to treat as one instrument? (method/confidence + rules)
  - risk_inputs  : Bernoulli terminal variance p(1-p) + time-to-resolution T, the inputs an
                   Avellaneda-Stoikov / GL-FT maker consumes (see as_reference_quote for how)

We supply the inputs the maker's model needs; the maker keeps its own edge (quoting, sizing,
execution). "The data layer, not the edge."

stdlib only. Feed-agnostic: the two Legs come from the live matched/equivalents feed.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from pytheum.mm.resolution_fields import divergence_from_text

# A pair whose only match evidence is an LLM judgement below this confidence is NOT safe to
# quote tight against as a hedge — resolution semantics may differ (the fungibility gate).
_FUNGIBLE_CONF_FLOOR = 0.90
# Deterministic/structural match methods that resolve the SAME question by construction.
_DETERMINISTIC_METHODS = frozenset({
    "structured_key", "game_match", "game_title_match", "spread_match",
    "deterministic", "human_adjudicated",
})


@dataclass(frozen=True)
class Leg:
    """One venue's quote on a matched market. implied_yes is the mid/last probability; the book
    fields are the live top-of-book (used to weight the reference + gauge executable size).

    IMPORTANT: implied_yes and bid/ask must already be in a COMMON frame across the two legs
    (Kalshi-YES). When the cross-venue side-map says the PM leg tracks the opposite token, the
    caller re-orients before constructing the Leg (mm reference does this from the /equivalents
    cross_venue block, which is oriented server-side)."""
    venue: str
    implied_yes: float | None = None
    bid: float | None = None
    ask: float | None = None
    bid_size: float | None = None
    ask_size: float | None = None

    def mid(self) -> float | None:
        if isinstance(self.bid, (int, float)) and isinstance(self.ask, (int, float)):
            return (self.bid + self.ask) / 2.0
        return self.implied_yes

    def spread(self) -> float | None:
        if isinstance(self.bid, (int, float)) and isinstance(self.ask, (int, float)):
            return max(self.ask - self.bid, 0.0)
        return None

    def top_size(self) -> float:
        return min(s for s in (self.bid_size, self.ask_size) if isinstance(s, (int, float))) \
            if any(isinstance(s, (int, float)) for s in (self.bid_size, self.ask_size)) else 0.0


def _leg_weight(leg: Leg) -> float:
    """Informativeness weight for the reference blend: a TIGHTER, DEEPER quote is a more
    confident estimate of true value, so weight ~ size / spread (inverse-variance intuition).
    Falls back to a small positive weight when depth/spread are unknown so a mid-only leg still
    contributes."""
    if leg.mid() is None:
        return 0.0
    spr = leg.spread()
    size = leg.top_size()
    if spr is not None and spr > 0:
        return (1.0 + size) / spr           # tighter + deeper → larger weight
    return 1.0                                # mid-only: neutral unit weight


def reference_fair_value(kalshi: Leg, pm: Leg) -> tuple[float | None, float | None]:
    """(p_hat, basis). p_hat = tightness/depth-weighted blend of the two legs' mids — the
    consolidated 'NBBO of prediction markets'. basis = kalshi_mid - pm_mid (the divergence a
    maker uses as the informed cross-venue signal). Either is None when the input is missing."""
    km, pmm = kalshi.mid(), pm.mid()
    basis = (km - pmm) if (km is not None and pmm is not None) else None
    wk, wp = _leg_weight(kalshi), _leg_weight(pm)
    if wk + wp <= 0:
        return None, basis
    parts = [(km, wk), (pmm, wp)]
    num = sum(m * w for m, w in parts if m is not None)
    den = sum(w for m, w in parts if m is not None)
    p_hat = round(num / den, 6) if den > 0 else None
    return p_hat, (round(basis, 6) if basis is not None else None)


@dataclass(frozen=True)
class Fungibility:
    """Is the matched pair safe to treat as ONE instrument (hedge/anchor)?"""
    fungible: bool
    confidence: float | None
    method: str | None
    reason: str


def fungibility(method: str | None, confidence: float | None,
                settlement_divergence: bool = False) -> Fungibility:
    """Verdict on whether the two legs resolve the SAME question. Deterministic/structural
    matches are fungible by construction; LLM-judged matches must clear a confidence floor;
    an explicit settlement-divergence flag (from the structured resolution fields) always
    vetoes. This is the gate that stops a maker quoting tight on a pair that can resolve
    differently — the #1 PM MM risk."""
    m = (method or "").strip().lower()
    if settlement_divergence:
        return Fungibility(False, confidence, method,
                           "settlement-divergence flag set — legs can resolve differently")
    if m in _DETERMINISTIC_METHODS:
        return Fungibility(True, confidence, method, f"deterministic match ({m})")
    if isinstance(confidence, (int, float)) and confidence >= _FUNGIBLE_CONF_FLOOR:
        return Fungibility(True, confidence, method,
                           f"judged match, confidence {confidence:.2f} >= {_FUNGIBLE_CONF_FLOOR}")
    return Fungibility(False, confidence, method,
                       "judged match below confidence floor — confirm resolution rules before hedging")


def terminal_variance(p: float | None) -> float | None:
    """Bernoulli terminal variance p(1-p): the variance of the 0/1 payoff at resolution. Bounded,
    known, max at 0.5, -> 0 near 0/1. Replaces diffusion sigma^2 in the PM-adapted A-S model."""
    if not isinstance(p, (int, float)):
        return None
    return round(p * (1.0 - p), 6)


def time_to_resolution_years(resolution_at: str | datetime | None,
                             now: datetime | None = None) -> float | None:
    """Years until resolution (A-S's horizon T is literally the resolution timestamp)."""
    if resolution_at is None:
        return None
    if isinstance(resolution_at, str):
        try:
            resolution_at = datetime.fromisoformat(resolution_at.replace("Z", "+00:00"))
        except ValueError:
            return None
    if resolution_at.tzinfo is None:
        resolution_at = resolution_at.replace(tzinfo=UTC)
    now = now or datetime.now(UTC)
    secs = (resolution_at - now).total_seconds()
    return round(max(secs, 0.0) / (365.0 * 24 * 3600), 8)


def as_reference_quote(p_hat: float, inventory: float, T_years: float, *,
                       gamma: float, kappa: float) -> dict[str, float]:
    """Reference implementation of the PM-adapted Avellaneda-Stoikov / GL-FT quote — the maker
    supplies its OWN calibrated ``gamma``/``kappa``/``inventory``, so this is the formula, not a
    placeholder. Deliberately NOT invoked by the served ``mm_reference`` endpoint (which returns
    only objective inputs, never a quote computed from parameters we don't own); retained as the
    canonical A-S adaptation for a caller/SDK that has its own calibration.

    PM-adapted: uses the Bernoulli terminal variance p(1-p) in place of diffusion sigma^2, and T
    is the real resolution horizon. reservation r = p_hat - inventory*gamma*p(1-p)*T (skew
    AGAINST inventory); optimal spread = gamma*p(1-p)*T + (2/gamma)*ln(1+gamma/kappa). Prices
    clipped to [0,1] (a probability can't leave the unit interval)."""
    var = p_hat * (1.0 - p_hat)
    reservation = p_hat - inventory * gamma * var * T_years
    half_spread = 0.5 * (gamma * var * T_years + (2.0 / gamma) * math.log1p(gamma / kappa))
    bid = max(0.0, min(1.0, reservation - half_spread))
    ask = max(0.0, min(1.0, reservation + half_spread))
    return {
        "reservation_price": round(reservation, 6),
        "half_spread": round(half_spread, 6),
        "bid": round(bid, 6), "ask": round(ask, 6),
        "inventory_skew": round(reservation - p_hat, 6),
    }


def advise(kalshi: Leg, pm: Leg, *, resolution_at: str | datetime | None = None,
           method: str | None = None, confidence: float | None = None,
           settlement_divergence: bool = False,
           kalshi_rules: str | None = None, pm_rules: str | None = None,
           now: datetime | None = None) -> dict[str, Any]:
    """The full MM-reference record for a matched pair: reference fair value, basis, fungibility
    verdict, and the A-S/GL-FT risk inputs (terminal variance + T). Warnings flag what a maker
    must not do (quote a non-fungible pair as a hedge; trust a reference with one dead leg).

    When both legs' resolution-rules text is supplied, settlement divergence is DETECTED from the
    rules (resolution_fields.divergence_from_text) — a hard signal that upgrades the fungibility
    verdict beyond the method/confidence proxy — OR-ed with any explicitly-passed flag."""
    rules_divergent, div_reasons = divergence_from_text(kalshi_rules, pm_rules)
    settlement_divergence = settlement_divergence or rules_divergent

    p_hat, basis = reference_fair_value(kalshi, pm)
    fung = fungibility(method, confidence, settlement_divergence)
    T = time_to_resolution_years(resolution_at, now)
    var = terminal_variance(p_hat)
    # The gamma- and inventory-free kernel of the PM-adapted Avellaneda-Stoikov maker:
    # reservation skew = -inventory * gamma * gradient; spread risk term = gamma * gradient.
    # This is the piece Pytheum owns from data (settlement-verified fair value -> p(1-p),
    # real resolution date -> T); the maker multiplies by its own gamma/inventory/kappa.
    gradient = round(var * T, 8) if (var is not None and T is not None) else None
    warnings: list[str] = []
    if not fung.fungible:
        warnings.append("not_fungible: " + fung.reason)
    for r in div_reasons:
        warnings.append("settlement_divergence: " + r)
    if kalshi.mid() is None or pm.mid() is None:
        warnings.append("one_leg_missing: reference is single-venue, not cross-venue")
    if basis is not None and abs(basis) > 0.05:
        warnings.append(f"wide_basis: {basis:+.3f} — informed signal or stale leg, verify before quoting")
    return {
        "p_hat": p_hat,
        "basis": basis,
        "fungibility": {"fungible": fung.fungible, "confidence": fung.confidence,
                        "method": fung.method, "reason": fung.reason},
        "risk_inputs": {
            "terminal_variance": var,
            "time_to_resolution_years": T,
            "inventory_risk_gradient": gradient,
        },
        "legs": {"kalshi": kalshi.mid(), "polymarket": pm.mid()},
        "warnings": warnings,
    }
