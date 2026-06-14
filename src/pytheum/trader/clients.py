"""Venue client holder for the trader-data layer.

Clients are constructed ONCE at server start() with no auth credentials
(public endpoints only — no trading keys). Closed at stop().

Note: pytheum-core is an optional runtime dependency; pin it in pyproject.toml
to enable the live trader-data layer.
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pytheum_core.venues.kalshi.client import KalshiClient
    from pytheum_core.venues.polymarket.client import PolymarketClient

logger = logging.getLogger(__name__)

__all__ = ["TraderClients"]


class TraderClients:
    """Holds one KalshiClient and one PolymarketClient (both public, unauthenticated).

    Lifecycle: call ``start()`` once after the event loop is running; call
    ``stop()`` at clean shutdown.  Handlers may access ``.kalshi`` and
    ``.polymarket`` directly after start(); both are None before start().
    """

    def __init__(self) -> None:
        self.kalshi: KalshiClient | None = None
        self.polymarket: PolymarketClient | None = None

    async def start(self) -> None:
        """Construct clients.  Safe to call multiple times (idempotent)."""
        from pytheum_core.venues.kalshi.client import KalshiClient
        from pytheum_core.venues.polymarket.client import PolymarketClient

        if self.kalshi is None:
            self.kalshi = KalshiClient()   # no signer → public endpoints only
        if self.polymarket is None:
            self.polymarket = PolymarketClient()  # no signer → public endpoints only
        logger.info("trader clients started (public, unauthenticated)")

    async def stop(self) -> None:
        """Close HTTP connections for both clients."""
        if self.kalshi is not None:
            await self.kalshi.aclose()
            self.kalshi = None
        if self.polymarket is not None:
            await self.polymarket.aclose()
            self.polymarket = None
        logger.info("trader clients stopped")

    @property
    def ready(self) -> bool:
        return self.kalshi is not None and self.polymarket is not None


def _get_clients(clients: Any) -> tuple[Any, Any]:
    """Extract (kalshi_client, polymarket_client) from a TraderClients or duck-typed stub."""
    return getattr(clients, "kalshi", None), getattr(clients, "polymarket", None)
