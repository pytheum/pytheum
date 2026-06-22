"""GET /v1/markets/{ref}/oi — open interest snapshot.

Kalshi:    core KalshiRest.get_market(ticker) — extract ``open_interest`` field
Polymarket: resolve ref → condition_id, then data.get_open_interest([condition_id])

Normalised output:
  {open_interest: float|None, venue, ref, source:"live"}

Error degradation: any venue/network error returns
  200 with {error, detail, source:"unavailable", venue, ref}
"""
from __future__ import annotations

import logging
from typing import Any

from pytheum.api.ref_utils import normalize_ref
from pytheum.trader.cache import _TTL_OI, SingleFlightCache
from pytheum.trader.normalizers import normalize_kalshi_oi, normalize_pm_oi
from pytheum.trader.resolve import PmResolved, kalshi_ticker_from_ref, resolve_pm

logger = logging.getLogger(__name__)

__all__ = ["handle_market_oi"]

_cache = SingleFlightCache()
_TTL_RESOLVE: float = 300.0


def _error_response(ref: str, venue: str, exc: BaseException) -> tuple[int, dict[str, Any]]:
    return 200, {
        "error": "venue_unavailable",
        "detail": str(exc)[:300],
        "source": "unavailable",
        "venue": venue,
        "ref": ref,
    }


async def handle_market_oi(
    ref: str,
    query: dict[str, str],
    *,
    clients: Any,
    _cache: SingleFlightCache = _cache,
) -> tuple[int, dict[str, Any]]:
    """GET /v1/markets/{ref}/oi handler."""
    ref_norm = normalize_ref(ref)
    head, sep, body = ref_norm.partition(":")
    venue = head.lower() if sep else ""

    cache_key = ("oi", ref_norm)

    if venue == "kalshi":
        kalshi_client = getattr(clients, "kalshi", None)
        if kalshi_client is None:
            return 200, {"error": "clients_not_ready", "source": "unavailable", "ref": ref_norm}
        ticker = kalshi_ticker_from_ref(ref_norm)

        async def _fetch_kalshi() -> dict[str, Any]:
            raw_body, _env = await kalshi_client.rest.get_market(ticker)
            return normalize_kalshi_oi(raw_body, ref=ref_norm)

        try:
            result = await _cache.get_or_fetch(
                cache_key, _TTL_OI, _fetch_kalshi, venue="kalshi"
            )
        except Exception as exc:
            logger.warning("kalshi oi fetch failed ref=%s: %s", ref_norm, exc)
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
            resolved: PmResolved = await _cache.get_or_fetch(
                resolve_key, _TTL_RESOLVE, _resolve, venue="polymarket"
            )
        except Exception as exc:
            logger.warning("pm token resolve failed ref=%s: %s", ref_norm, exc)
            return _error_response(ref_norm, "polymarket", exc)

        async def _fetch_pm() -> dict[str, Any]:
            items, _env = await pm_client.data.get_open_interest([resolved.condition_id])
            return normalize_pm_oi(items, ref=ref_norm)

        try:
            result = await _cache.get_or_fetch(
                cache_key, _TTL_OI, _fetch_pm, venue="polymarket"
            )
        except Exception as exc:
            logger.warning("pm oi fetch failed ref=%s: %s", ref_norm, exc)
            return _error_response(ref_norm, "polymarket", exc)
        return 200, result

    else:
        return 200, {
            "error": "unknown_venue",
            "detail": f"ref must be venue-prefixed (kalshi:… or polymarket:…), got {ref_norm!r}",
            "ref": ref_norm,
        }
