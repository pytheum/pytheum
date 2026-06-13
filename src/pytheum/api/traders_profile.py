"""GET /v1/traders/{wallet} — Polymarket trader profile.

Merges positions + recent activity + portfolio value into one response.
Polymarket-only: Kalshi trades are anonymized.
TTL: 60s. Coalesced via SingleFlightCache.

wallet: 0x-hex address (e.g. Ethereum/proxy wallet) or Polymarket username.
"""
from __future__ import annotations

import asyncio
import logging
import re
from typing import Any

from pytheum.trader.cache import SingleFlightCache
from pytheum.trader.normalizers import (
    normalize_pm_activity,
    normalize_pm_positions,
    normalize_pm_value,
)

logger = logging.getLogger(__name__)

__all__ = ["handle_trader_profile"]

_cache = SingleFlightCache()
_TTL_PROFILE: float = 60.0

# Accept: 0x + at least 10 hex chars (covers full 40-char EVM addresses + proxy
# wallets); OR an alphanumeric Polymarket username (3–64 chars).
_WALLET_RE = re.compile(r"^(0x[0-9a-fA-F]{10,}|[A-Za-z0-9_\-\.]{3,64})$")


def _valid_wallet(wallet: str) -> bool:
    return bool(_WALLET_RE.match(wallet.strip()))


def _error_response(wallet: str, exc: BaseException) -> tuple[int, dict[str, Any]]:
    return 200, {
        "error": "venue_unavailable",
        "detail": str(exc)[:300],
        "source": "unavailable",
        "venue": "polymarket",
        "wallet": wallet,
    }


async def handle_trader_profile(
    wallet: str,
    query: dict[str, str],
    *,
    clients: Any,
    _cache: SingleFlightCache = _cache,
) -> tuple[int, dict[str, Any]]:
    """GET /v1/traders/{wallet} handler. Polymarket-only."""
    pm_client = getattr(clients, "polymarket", None)
    if pm_client is None:
        return 200, {
            "error": "clients_not_ready",
            "source": "unavailable",
            "venue": "polymarket",
            "wallet": wallet,
        }

    if not _valid_wallet(wallet):
        return 200, {
            "error": "invalid_wallet",
            "detail": (
                "wallet must be a 0x-hex address (e.g. '0xabc…' 42 chars) "
                "or a Polymarket username (3–64 alphanumeric chars)"
            ),
            "wallet": wallet,
        }

    cache_key = ("trader_profile", wallet)

    async def _fetch() -> dict[str, Any]:
        positions_result, activity_result, value_result = await asyncio.gather(
            pm_client.data.get_positions(user=wallet),
            pm_client.data.get_activity(user=wallet, limit=50),
            pm_client.data.get_value(user=wallet, limit=1),
        )
        positions_items, _ = positions_result
        activity_items, _ = activity_result
        value_items, _ = value_result
        return {
            "wallet": wallet,
            "positions": normalize_pm_positions(positions_items),
            "activity": normalize_pm_activity(activity_items),
            "value": normalize_pm_value(value_items),
            "meta": {
                "source": "live",
                "venue": "polymarket",
                "note": "Polymarket public analytics. Kalshi trades are anonymized.",
            },
        }

    try:
        result = await _cache.get_or_fetch(cache_key, _TTL_PROFILE, _fetch)
    except Exception as exc:
        logger.warning("pm trader profile fetch failed wallet=%s: %s", wallet, exc)
        return _error_response(wallet, exc)
    return 200, result
