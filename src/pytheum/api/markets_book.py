"""GET /v1/markets/{ref}/book?depth=20 — live orderbook snapshot.

Kalshi:  core KalshiRest.get_orderbook(ticker, depth=depth)
Polymarket: resolve ref → CLOB token_id via Gamma, then clob.get_book(token_id)

Normalised output:
  {bids: [[price, size], ...], asks: [...], venue, ref, ts, source:"live", top:{...}}

Error degradation (never-500 convention): any venue/network error returns
  200 with {error, detail, source:"unavailable", venue, ref}
"""
from __future__ import annotations

import logging
from typing import Any

from pytheum.api.ref_utils import normalize_ref
from pytheum.trader.cache import _TTL_BOOK, SingleFlightCache
from pytheum.trader.normalizers import normalize_kalshi_book, normalize_pm_book
from pytheum.trader.resolve import PmResolved, kalshi_ticker_from_ref, resolve_pm

logger = logging.getLogger(__name__)

__all__ = ["handle_market_book"]

# Module-level cache — one instance shared across all requests for the lifetime
# of the server process.
_cache = SingleFlightCache()

# TTL for PM token-id resolution: token IDs are immutable so we cache them long.
_TTL_RESOLVE: float = 300.0


def _parse_depth(query: dict[str, str]) -> int:
    try:
        return max(1, min(int(query.get("depth", 20)), 200))
    except (ValueError, TypeError):
        return 20


def _error_response(
    ref: str,
    venue: str,
    exc: BaseException,
) -> tuple[int, dict[str, Any]]:
    return 200, {
        "error": "venue_unavailable",
        "detail": str(exc)[:300],
        "source": "unavailable",
        "venue": venue,
        "ref": ref,
    }


async def handle_market_book(
    ref: str,
    query: dict[str, str],
    *,
    clients: Any,
    _cache: SingleFlightCache = _cache,  # injectable in tests
) -> tuple[int, dict[str, Any]]:
    """GET /v1/markets/{ref}/book handler."""
    depth = _parse_depth(query)
    ref_norm = normalize_ref(ref)
    head, sep, body = ref_norm.partition(":")
    venue = head.lower() if sep else ""

    cache_key = ("book", ref_norm, depth)

    if venue == "kalshi":
        kalshi_client = getattr(clients, "kalshi", None)
        if kalshi_client is None:
            return 200, {"error": "clients_not_ready", "source": "unavailable", "ref": ref_norm}
        ticker = kalshi_ticker_from_ref(ref_norm)

        async def _fetch_kalshi() -> dict[str, Any]:
            raw_body, _env = await kalshi_client.rest.get_orderbook(ticker, depth=depth)
            return normalize_kalshi_book(raw_body, ref=ref_norm, depth=depth)

        try:
            result = await _cache.get_or_fetch(
                cache_key, _TTL_BOOK, _fetch_kalshi, venue="kalshi"
            )
        except Exception as exc:
            logger.warning("kalshi book fetch failed ref=%s: %s", ref_norm, exc)
            return _error_response(ref_norm, "kalshi", exc)
        return 200, result

    elif venue == "polymarket":
        pm_client = getattr(clients, "polymarket", None)
        if pm_client is None:
            return 200, {"error": "clients_not_ready", "source": "unavailable", "ref": ref_norm}

        # Resolve token_id (cached separately with longer TTL)
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
            raw_body, _env = await pm_client.clob.get_book(resolved.token_id)
            return normalize_pm_book(raw_body, ref=ref_norm, depth=depth)

        try:
            result = await _cache.get_or_fetch(
                cache_key, _TTL_BOOK, _fetch_pm, venue="polymarket"
            )
        except Exception as exc:
            logger.warning("pm book fetch failed ref=%s: %s", ref_norm, exc)
            return _error_response(ref_norm, "polymarket", exc)
        return 200, result

    else:
        return 200, {
            "error": "unknown_venue",
            "detail": f"ref must be venue-prefixed (kalshi:… or polymarket:…), got {ref_norm!r}",
            "ref": ref_norm,
        }
