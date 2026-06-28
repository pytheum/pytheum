"""GET /v1/markets/whale-trades?min_usd=500&limit=50[&venue=…][&market_ref=…:…]

Large-notional recent trades, BOTH venues:
- Polymarket: wallet-level (trade carries the on-chain wallet/pseudonym).
- Kalshi: ANONYMOUS — centralized exchange exposes no trader/wallet identity, so this is
  pure size-based whale detection from the public trade tape (notional = size * price).

Mode is chosen by ``venue`` (default ``polymarket`` for back-compat) and/or ``market_ref``:
- no ``market_ref`` → GLOBAL recent-whale feed across all of that venue's markets;
- ``market_ref`` given → that one market (its prefix also fixes the venue).

TTL 30s, coalesced via SingleFlightCache (K concurrent identical requests → 1 venue fetch).
Register this route BEFORE any /{ref} catch-all routes to avoid shadowing.
On any venue error returns 200 with {error, detail, source:"unavailable"}.
"""
from __future__ import annotations

import logging
from typing import Any

from pytheum.api.ref_utils import normalize_ref
from pytheum.trader.cache import SingleFlightCache
from pytheum.trader.normalizers import (
    normalize_kalshi_whale_trades,
    normalize_pm_whale_trades,
)
from pytheum.trader.resolve import PmResolved, kalshi_ticker_from_ref, resolve_pm

logger = logging.getLogger(__name__)

__all__ = ["handle_market_whale_trades"]

_cache = SingleFlightCache()
_TTL_WHALE: float = 30.0
_TTL_RESOLVE: float = 300.0

# Over-fetch so post-fetch notional filtering doesn't starve the page.
# Polymarket: the data tape is already trade-dense.
_RAW_LIMIT_MULT = 10
_RAW_LIMIT_MAX = 500
# Kalshi: whales are a small fraction of the global tape, so scan deeper (cap = Kalshi's
# documented /markets/trades page max of 1000). Still ONE upstream call — no per-market fan-out.
_RAW_LIMIT_MULT_KALSHI = 20
_RAW_LIMIT_MAX_KALSHI = 1000

_VENUES = ("polymarket", "kalshi")


def _parse_min_usd(query: dict[str, str]) -> float:
    try:
        return max(0.0, float(query.get("min_usd", "500")))
    except (ValueError, TypeError):
        return 500.0


def _parse_limit(query: dict[str, str]) -> int:
    try:
        return max(1, min(int(query.get("limit", "50")), 500))
    except (ValueError, TypeError):
        return 50


def _error_response(
    exc: BaseException, *, ref: str | None = None, venue: str = "polymarket"
) -> tuple[int, dict[str, Any]]:
    body: dict[str, Any] = {
        "error": "venue_unavailable",
        "detail": str(exc)[:300],
        "source": "unavailable",
        "venue": venue,
    }
    if ref is not None:
        body["ref"] = ref
    return 200, body


async def handle_market_whale_trades(
    query: dict[str, str],
    *,
    clients: Any,
    _cache: SingleFlightCache = _cache,
) -> tuple[int, dict[str, Any]]:
    """GET /v1/markets/whale-trades handler. Venue-aware (Polymarket + Kalshi)."""
    min_usd = _parse_min_usd(query)
    limit = _parse_limit(query)
    market_ref_raw: str | None = query.get("market_ref")
    venue: str | None = (query.get("venue") or "").strip().lower() or None

    ref_norm: str | None = None
    if market_ref_raw:
        ref_norm = normalize_ref(market_ref_raw)
        head, sep, _body = ref_norm.partition(":")
        ref_venue = head.lower() if sep else None
        if ref_venue not in _VENUES:
            return 200, {
                "error": "invalid_market_ref",
                "detail": "market_ref must be venue-prefixed ('polymarket:…' or 'kalshi:…').",
                "ref": ref_norm,
            }
        if venue and venue != ref_venue:
            return 200, {
                "error": "venue_mismatch",
                "detail": f"venue='{venue}' but market_ref is '{ref_venue}'.",
                "ref": ref_norm,
            }
        venue = ref_venue
    venue = venue or "polymarket"  # back-compat default (PM was the original behavior)

    if venue not in _VENUES:
        return 200, {"error": "invalid_venue", "detail": f"venue must be one of {_VENUES}."}

    if venue == "kalshi":
        return await _whale_kalshi(clients, ref_norm, min_usd, limit, _cache)
    return await _whale_polymarket(clients, ref_norm, min_usd, limit, _cache)


async def _whale_polymarket(
    clients: Any, ref_norm: str | None, min_usd: float, limit: int, _cache: SingleFlightCache
) -> tuple[int, dict[str, Any]]:
    pm_client = getattr(clients, "polymarket", None)
    if pm_client is None:
        return 200, {"error": "clients_not_ready", "source": "unavailable", "venue": "polymarket"}

    condition_id: str | None = None
    if ref_norm:
        _head, _sep, body_str = ref_norm.partition(":")
        resolve_key = ("resolve_pm", ref_norm)

        async def _resolve() -> PmResolved:
            return await resolve_pm(body_str, gamma=pm_client.gamma)

        try:
            resolved: PmResolved = await _cache.get_or_fetch(
                resolve_key, _TTL_RESOLVE, _resolve, venue="polymarket"
            )
            condition_id = resolved.condition_id
        except Exception as exc:
            logger.warning("pm resolve failed for whale-trades ref=%s: %s", ref_norm, exc)
            return _error_response(exc, ref=ref_norm)

    raw_limit = min(limit * _RAW_LIMIT_MULT, _RAW_LIMIT_MAX)
    cache_key = ("whale_trades", condition_id, min_usd, limit)

    async def _fetch() -> dict[str, Any]:
        markets = [condition_id] if condition_id else None
        items, _env = await pm_client.data.get_trades(markets=markets, limit=raw_limit)
        whales = normalize_pm_whale_trades(items, min_usd=min_usd, limit=limit, ref=ref_norm)
        out: dict[str, Any] = {
            "trades": whales,
            "count": len(whales),
            "min_usd": min_usd,
            "venue": "polymarket",
            "source": "live",
            "note": "Polymarket — wallet-level whales (on-chain identity).",
        }
        if ref_norm is not None:
            out["ref"] = ref_norm
        return out

    try:
        result = await _cache.get_or_fetch(cache_key, _TTL_WHALE, _fetch, venue="polymarket")
    except Exception as exc:
        logger.warning("pm whale trades fetch failed min_usd=%s: %s", min_usd, exc)
        return _error_response(exc, ref=ref_norm)
    return 200, result


async def _whale_kalshi(
    clients: Any, ref_norm: str | None, min_usd: float, limit: int, _cache: SingleFlightCache
) -> tuple[int, dict[str, Any]]:
    kalshi = getattr(clients, "kalshi", None)
    if kalshi is None:
        return 200, {"error": "clients_not_ready", "source": "unavailable", "venue": "kalshi"}

    # ticker=None → Kalshi GLOBAL recent-trades tape (cross-market whale feed); else one market.
    ticker = kalshi_ticker_from_ref(ref_norm) if ref_norm else None
    raw_limit = min(limit * _RAW_LIMIT_MULT_KALSHI, _RAW_LIMIT_MAX_KALSHI)
    cache_key = ("whale_trades_kalshi", ticker, min_usd, limit)

    async def _fetch() -> dict[str, Any]:
        body, _env, _cursor = await kalshi.rest.get_trades_page(ticker, limit=raw_limit)
        whales = normalize_kalshi_whale_trades(body, min_usd=min_usd, limit=limit)
        out: dict[str, Any] = {
            "trades": whales,
            "count": len(whales),
            "min_usd": min_usd,
            "venue": "kalshi",
            "source": "live",
            "note": ("Kalshi — anonymous, size-based whales only; the venue exposes no "
                     "trader/wallet identity, so trades carry no 'wallet' field."),
        }
        if ref_norm is not None:
            out["ref"] = ref_norm
        return out

    try:
        result = await _cache.get_or_fetch(cache_key, _TTL_WHALE, _fetch, venue="kalshi")
    except Exception as exc:
        logger.warning("kalshi whale trades fetch failed min_usd=%s ticker=%s: %s",
                       min_usd, ticker, exc)
        return _error_response(exc, ref=ref_norm, venue="kalshi")
    return 200, result
