"""GET /v1/markets/whale-trades?min_usd=500&limit=50[&market_ref=polymarket:…]

Polymarket-only: Kalshi trades are anonymized.
Fetches recent trades and filters to notional_usd (size * price) >= min_usd.
TTL: 30s. Coalesced via SingleFlightCache.

market_ref is an optional query-parameter filter (venue-prefixed polymarket ref).
Register this route BEFORE any /{ref} catch-all routes to avoid shadowing.

On any venue error returns 200 with {error, detail, source:"unavailable"}.
"""
from __future__ import annotations

import logging
from typing import Any

from pytheum.api.ref_utils import normalize_ref
from pytheum.trader.cache import SingleFlightCache
from pytheum.trader.normalizers import normalize_pm_whale_trades
from pytheum.trader.resolve import PmResolved, resolve_pm

logger = logging.getLogger(__name__)

__all__ = ["handle_market_whale_trades"]

_cache = SingleFlightCache()
_TTL_WHALE: float = 30.0
_TTL_RESOLVE: float = 300.0

# Over-fetch multiplier so filtering doesn't starve the result set.
_RAW_LIMIT_MULT = 10
_RAW_LIMIT_MAX = 500


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


def _error_response(exc: BaseException, *, ref: str | None = None) -> tuple[int, dict[str, Any]]:
    body: dict[str, Any] = {
        "error": "venue_unavailable",
        "detail": str(exc)[:300],
        "source": "unavailable",
        "venue": "polymarket",
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
    """GET /v1/markets/whale-trades handler. Polymarket-only."""
    pm_client = getattr(clients, "polymarket", None)
    if pm_client is None:
        return 200, {
            "error": "clients_not_ready",
            "source": "unavailable",
            "venue": "polymarket",
        }

    min_usd = _parse_min_usd(query)
    limit = _parse_limit(query)
    market_ref_raw: str | None = query.get("market_ref")

    # Optionally resolve market_ref → condition_id
    condition_id: str | None = None
    ref_norm: str | None = None

    if market_ref_raw:
        ref_norm = normalize_ref(market_ref_raw)
        head, sep, body_str = ref_norm.partition(":")
        if not sep or head.lower() != "polymarket":
            return 200, {
                "error": "polymarket_only",
                "detail": (
                    "Whale trades market_ref must be a Polymarket ref "
                    "(e.g. 'polymarket:some-slug'). Kalshi trades are anonymized."
                ),
                "ref": ref_norm,
            }

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

    # Cache key includes all filtering dimensions so different param combos are
    # independent entries (no result pollution across callers).
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
            "note": "Polymarket-only. Kalshi trades are anonymized.",
        }
        if ref_norm is not None:
            out["ref"] = ref_norm
        return out

    try:
        result = await _cache.get_or_fetch(
            cache_key, _TTL_WHALE, _fetch, venue="polymarket"
        )
    except Exception as exc:
        logger.warning("pm whale trades fetch failed min_usd=%s: %s", min_usd, exc)
        return _error_response(exc, ref=ref_norm)
    return 200, result
