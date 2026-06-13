"""PM token-id + condition-id resolution from stream refs.

Chosen path (documented here per the task requirement):

  The stream uses three PM ref formats:
    polymarket:<numeric>       → Gamma numeric market ID   (get_market_by_id)
    polymarket:0x<hex>         → on-chain condition_id     (get_market_by_condition_id)
    polymarket:<slug>          → Gamma market slug         (get_market_by_slug)

  Every PM ref that is NOT already a condition_id (0x…) goes through Gamma to
  retrieve:
    • clobTokenIds[0]  — the YES token_id needed for CLOB orderbook / trades
    • conditionId      — the condition ID needed for the Data API OI endpoint

  This is the robust path: the Gamma response is always authoritative; we do
  NOT attempt to extract clobTokenIds from DAO payload (which might be stale or
  absent for markets not ingested via stream).  Resolution results are cached via
  the caller's SingleFlightCache with a long TTL (300 s) since token IDs and
  condition IDs are immutable.

  For Kalshi refs:  the body of `kalshi:<ticker>` is the ticker verbatim —
  no resolution needed.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pytheum_core.venues.polymarket.gamma import PolymarketGammaRest

__all__ = ["PmResolved", "resolve_pm", "kalshi_ticker_from_ref"]

# Long-TTL for token/condition ID resolution — these values are immutable.
_TTL_RESOLUTION: float = 300.0


@dataclass(frozen=True)
class PmResolved:
    """Token-id (for CLOB) and condition-id (for Data API) for a PM market."""
    token_id: str     # YES token_id — CLOB /book and /trades
    condition_id: str # condition_id  — Data API /open-interest


def _is_condition_id(s: str) -> bool:
    """True when the body looks like a 0x… on-chain condition ID."""
    return s.startswith("0x") and len(s) > 10


def _is_numeric_gamma_id(s: str) -> bool:
    return s.isdigit()


def _extract_resolved(market: dict[str, Any]) -> PmResolved:
    """Extract token_id + condition_id from a Gamma market dict."""
    clob_ids: list[str] | None = market.get("clobTokenIds")
    if not clob_ids:
        raise ValueError(
            f"Gamma market has no clobTokenIds — cannot resolve to CLOB token_id. "
            f"Market: {market.get('id')!r}"
        )
    token_id = str(clob_ids[0])
    condition_id = str(market.get("conditionId") or "")
    if not condition_id:
        raise ValueError(
            f"Gamma market has no conditionId — cannot resolve for Data API. "
            f"Market: {market.get('id')!r}"
        )
    return PmResolved(token_id=token_id, condition_id=condition_id)


async def resolve_pm(
    body: str,
    *,
    gamma: PolymarketGammaRest,
) -> PmResolved:
    """Resolve a PM ref body string to a PmResolved (token_id + condition_id).

    `body` is the part after 'polymarket:' — e.g. '0x849a3e…', '569356', or
    'will-the-fed-cut-rates'.

    Raises ValueError if Gamma cannot provide the required fields.
    """
    if _is_condition_id(body):
        market, _env = await gamma.get_market_by_condition_id(body)
    elif _is_numeric_gamma_id(body):
        market, _env = await gamma.get_market_by_id(market_id=body)
    else:
        # Treat as slug
        market, _env = await gamma.get_market_by_slug(body)

    return _extract_resolved(market)


def kalshi_ticker_from_ref(ref: str) -> str:
    """Extract the raw Kalshi ticker from 'kalshi:<ticker>' or bare '<ticker>'."""
    if ref.lower().startswith("kalshi:"):
        return ref[len("kalshi:"):]
    return ref
