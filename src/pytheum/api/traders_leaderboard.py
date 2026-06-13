"""GET /v1/traders/leaderboard?period=weekly|monthly — Polymarket trader leaderboard.

Polymarket-only: Kalshi trades are anonymized (no equivalent ranking exists).
TTL: 300s. Coalesced via SingleFlightCache.

On any venue error returns 200 with {error, detail, source:"unavailable"}.
"""
from __future__ import annotations

import logging
from typing import Any

from pytheum.trader.cache import SingleFlightCache
from pytheum.trader.normalizers import normalize_pm_leaderboard

logger = logging.getLogger(__name__)

__all__ = ["handle_traders_leaderboard"]

_cache = SingleFlightCache()
_TTL_LEADERBOARD: float = 300.0
_VALID_PERIODS = frozenset({"weekly", "monthly"})


def _parse_period(query: dict[str, str]) -> str:
    p = query.get("period", "weekly").strip().lower()
    return p if p in _VALID_PERIODS else "weekly"


def _error_response(exc: BaseException) -> tuple[int, dict[str, Any]]:
    return 200, {
        "error": "venue_unavailable",
        "detail": str(exc)[:300],
        "source": "unavailable",
        "venue": "polymarket",
    }


async def handle_traders_leaderboard(
    query: dict[str, str],
    *,
    clients: Any,
    _cache: SingleFlightCache = _cache,
) -> tuple[int, dict[str, Any]]:
    """GET /v1/traders/leaderboard handler. Polymarket-only."""
    pm_client = getattr(clients, "polymarket", None)
    if pm_client is None:
        return 200, {
            "error": "clients_not_ready",
            "source": "unavailable",
            "venue": "polymarket",
        }

    period = _parse_period(query)
    cache_key = ("leaderboard", period)

    async def _fetch() -> dict[str, Any]:
        items, _env = await pm_client.data.get_leaderboard(period=period)
        return normalize_pm_leaderboard(items, period=period)

    try:
        result = await _cache.get_or_fetch(cache_key, _TTL_LEADERBOARD, _fetch)
    except Exception as exc:
        logger.warning("pm leaderboard fetch failed period=%s: %s", period, exc)
        return _error_response(exc)
    return 200, result
