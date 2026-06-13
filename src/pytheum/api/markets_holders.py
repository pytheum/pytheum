"""GET /v1/markets/{ref}/holders — Polymarket token holders for a market.

Polymarket-only: Kalshi trade data is anonymized.
Resolves ref → condition_id via Gamma (same resolve path as /book, /trades, /oi).
TTL: 60s. Coalesced via SingleFlightCache.

On any venue error returns 200 with {error, detail, source:"unavailable"}.
"""
from __future__ import annotations

import logging
from typing import Any

from pytheum.api.ref_utils import normalize_ref
from pytheum.trader.cache import SingleFlightCache
from pytheum.trader.normalizers import normalize_pm_holders
from pytheum.trader.resolve import PmResolved, resolve_pm

logger = logging.getLogger(__name__)

__all__ = ["handle_market_holders"]

_cache = SingleFlightCache()
_TTL_HOLDERS: float = 60.0
_TTL_RESOLVE: float = 300.0


def _error_response(ref: str, exc: BaseException) -> tuple[int, dict[str, Any]]:
    return 200, {
        "error": "venue_unavailable",
        "detail": str(exc)[:300],
        "source": "unavailable",
        "venue": "polymarket",
        "ref": ref,
    }


async def handle_market_holders(
    ref: str,
    query: dict[str, str],
    *,
    clients: Any,
    _cache: SingleFlightCache = _cache,
) -> tuple[int, dict[str, Any]]:
    """GET /v1/markets/{ref}/holders handler. Polymarket-only."""
    ref_norm = normalize_ref(ref)
    head, sep, body = ref_norm.partition(":")
    venue = head.lower() if sep else ""

    if venue != "polymarket":
        return 200, {
            "error": "polymarket_only",
            "detail": (
                "Holder analytics are Polymarket-only. "
                "Kalshi trade data is anonymized — no holder breakdown is available."
            ),
            "venue": venue or "unknown",
            "ref": ref_norm,
        }

    pm_client = getattr(clients, "polymarket", None)
    if pm_client is None:
        return 200, {
            "error": "clients_not_ready",
            "source": "unavailable",
            "venue": "polymarket",
            "ref": ref_norm,
        }

    # Resolve condition_id via Gamma (immutable — long TTL)
    resolve_key = ("resolve_pm", ref_norm)

    async def _resolve() -> PmResolved:
        return await resolve_pm(body, gamma=pm_client.gamma)

    try:
        resolved: PmResolved = await _cache.get_or_fetch(resolve_key, _TTL_RESOLVE, _resolve)
    except Exception as exc:
        logger.warning("pm token resolve failed ref=%s: %s", ref_norm, exc)
        return _error_response(ref_norm, exc)

    cache_key = ("holders", ref_norm)

    async def _fetch() -> dict[str, Any]:
        items, _env = await pm_client.data.get_holders(market=resolved.condition_id)
        return normalize_pm_holders(items, ref=ref_norm)

    try:
        result = await _cache.get_or_fetch(cache_key, _TTL_HOLDERS, _fetch)
    except Exception as exc:
        logger.warning("pm holders fetch failed ref=%s: %s", ref_norm, exc)
        return _error_response(ref_norm, exc)
    return 200, result
