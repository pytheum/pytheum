"""OhlcvProvider ABC + companion dataclasses (Stage 2, blueprint §2).

Implementations
---------------
VenueFallbackOhlcv  — venue native candle APIs (Kalshi / PM); source="venue_live"
PitArchiveOhlcv     — PIT archive + live-tape with VenueFallbackOhlcv as fallback;
                      source="pit_archive" | "mixed"
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from typing import Any

__all__ = ["OhlcvBar", "OhlcvProvider", "OhlcvResult"]


@dataclass
class OhlcvBar:
    """A single OHLCV candle bucket.

    Fields match the REST response shape:
      t       — ISO-Z bucket-start timestamp (e.g. '2026-06-01T00:00:00Z')
      o/h/l/c — open / high / low / close (yes-price, 4 d.p.)
      v       — volume (sum of n_trades in bucket), or None when unavailable
    """

    t: str
    o: float | None
    h: float | None
    low_price: float | None   # named low_price to avoid E741 ambiguous-name lint
    c: float | None
    v: float | None


@dataclass
class OhlcvResult:
    """Return value from OhlcvProvider.get_bars."""

    bars: list[dict[str, Any]]
    source: str                   # "pit_archive" | "venue_live" | "mixed"
    partial_last_bucket: bool


class OhlcvProvider(ABC):
    """Abstract provider for OHLCV candle data."""

    @abstractmethod
    async def get_bars(
        self,
        ref: str,
        interval: str,
        since: datetime,
        until: datetime,
        limit: int,
    ) -> OhlcvResult:
        """Return OHLCV candles for *ref* in the requested interval.

        Parameters
        ----------
        ref:      Normalised venue-prefixed market ref (e.g. 'kalshi:KXTICKER').
        interval: Label string from _INTERVALS — '1m', '5m', '15m', '1h', '1d'.
        since:    Inclusive start of the requested range (UTC).
        until:    Exclusive end of the requested range (UTC).
        limit:    Maximum number of candles to return (newest kept on clip).
        """

    @abstractmethod
    async def available_since(self, ref: str) -> datetime | None:
        """Earliest timestamp for which this provider has coverage, or None."""
