"""GET /v1/markets/{ref}/trades?limit=100 — recent trade tape.

Kalshi:    core KalshiRest.get_trades_page(ticker, limit=limit)
Polymarket: resolve ref → condition_id, then data.get_trades(markets=[condition_id], limit=limit)

Normalised output:
  {trades: [{ts, price, size, side}, ...], venue, ref, count, source:"live"}

Error degradation: any venue/network error returns
  200 with {error, detail, source:"unavailable", venue, ref}
"""
from __future__ import annotations

import logging
from typing import Any

from pytheum.api.ref_utils import normalize_ref
from pytheum.trader.cache import _TTL_TRADES, SingleFlightCache
from pytheum.trader.normalizers import normalize_kalshi_trades, normalize_pm_trades
from pytheum.trader.resolve import PmResolved, kalshi_ticker_from_ref, resolve_pm

logger = logging.getLogger(__name__)

__all__ = ["handle_market_trades"]

_cache = SingleFlightCache()
_TTL_RESOLVE: float = 300.0


def _parse_limit(query: dict[str, str]) -> int:
    try:
        return max(1, min(int(query.get("limit", 100)), 1000))
    except (ValueError, TypeError):
        return 100


def _error_response(ref: str, venue: str, exc: BaseException) -> tuple[int, dict[str, Any]]:
    return 200, {
        "error": "venue_unavailable",
        "detail": str(exc)[:300],
        "source": "unavailable",
        "venue": venue,
        "ref": ref,
    }


async def handle_market_trades(
    ref: str,
    query: dict[str, str],
    *,
    clients: Any,
    _cache: SingleFlightCache = _cache,
) -> tuple[int, dict[str, Any]]:
    """GET /v1/markets/{ref}/trades handler."""
    limit = _parse_limit(query)
    ref_norm = normalize_ref(ref)
    head, sep, body = ref_norm.partition(":")
    venue = head.lower() if sep else ""

    cache_key = ("trades", ref_norm, limit)

    if venue == "kalshi":
        kalshi_client = getattr(clients, "kalshi", None)
        if kalshi_client is None:
            return 200, {"error": "clients_not_ready", "source": "unavailable", "ref": ref_norm}
        ticker = kalshi_ticker_from_ref(ref_norm)

        async def _fetch_kalshi() -> dict[str, Any]:
            raw_body, _env, _cursor = await kalshi_client.rest.get_trades_page(
                ticker, limit=limit
            )
            trades = normalize_kalshi_trades(raw_body, limit=limit)
            return {"trades": trades, "venue": "kalshi", "ref": ref_norm,
                    "count": len(trades), "source": "live"}

        try:
            result = await _cache.get_or_fetch(cache_key, _TTL_TRADES, _fetch_kalshi)
        except Exception as exc:
            logger.warning("kalshi trades fetch failed ref=%s: %s", ref_norm, exc)
            return _error_response(ref_norm, "kalshi", exc)
        return 200, result

    elif venue == "polymarket":
        pm_client = getattr(clients, "polymarket", None)
        if pm_client is None:
            return 200, {"error": "clients_not_ready", "source": "unavailable", "ref": ref_norm}

        resolve_key = ("resolve_pm", ref_norm)

        async def _resolve() -> PmResolved:
            return await resolve_pm(body, gamma=pm_client.gamma)

        try:
            resolved: PmResolved = await _cache.get_or_fetch(resolve_key, _TTL_RESOLVE, _resolve)
        except Exception as exc:
            logger.warning("pm token resolve failed ref=%s: %s", ref_norm, exc)
            return _error_response(ref_norm, "polymarket", exc)

        async def _fetch_pm() -> dict[str, Any]:
            items, _env = await pm_client.data.get_trades(
                markets=[resolved.condition_id], limit=limit
            )
            trades = normalize_pm_trades(items, limit=limit)
            return {"trades": trades, "venue": "polymarket", "ref": ref_norm,
                    "count": len(trades), "source": "live"}

        try:
            result = await _cache.get_or_fetch(cache_key, _TTL_TRADES, _fetch_pm)
        except Exception as exc:
            logger.warning("pm trades fetch failed ref=%s: %s", ref_norm, exc)
            return _error_response(ref_norm, "polymarket", exc)
        return 200, result

    else:
        return 200, {
            "error": "unknown_venue",
            "detail": f"ref must be venue-prefixed (kalshi:… or polymarket:…), got {ref_norm!r}",
            "ref": ref_norm,
        }
