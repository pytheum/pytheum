"""GET /v1/markets/{ref}/ohlcv — OHLCV candles for a prediction market.

Primary source: pytheum's own PIT archive (market_price_series, minute-level
change-points distilled from raw tick data) + live tape (market_price_history),
resampled into OHLCV buckets. Backtest-grade: no lookahead; partial current
bucket is included but flagged so callers can exclude it.

Fall-through: when the archive has no coverage for the requested range, venue
candles are fetched via core through the existing SingleFlightCache pattern
(~60 s TTL, coalesced):
  Kalshi  → get_historical_candlesticks (1m/1h/1d native; 5m/15m aggregated
             from 1m; series_ticker is intentionally unused per core audit
             Finding #1 — we pass the ticker itself as a placeholder).
  PM      → get_prices_history (price-point series [{t, p}], resampled
             client-side using the same resample_to_ohlcv logic).

Source disclosure: "pit_archive" | "venue_live" | "mixed" (venue fills the
pre-archive gap when both sources contribute).

Response shape:
  {
    "market": {"id": "...", "question": "...", "venue": "..."},
    "interval": "1h",
    "candles": [{"t": "2026-06-01T00:00:00Z",
                 "o": 0.55, "h": 0.60, "l": 0.50, "c": 0.58, "v": 12}],
    "meta": {"source": "pit_archive", "count": 24, "partial_last_bucket": false}
  }

Never-500 convention: any error returns HTTP 200 with {error, hint}.

Stage 2 note: the data-fetching logic has been extracted into
  pytheum.ohlcv.resample      (resample_to_ohlcv)
  pytheum.ohlcv.venue_fallback (VenueFallbackOhlcv, _parse_kalshi_candles)

This module re-exports resample_to_ohlcv and _parse_kalshi_candles for
backwards compatibility with existing callers/tests.

Note (public repo): PitArchiveOhlcv lives in pytheum-pit (Stage 4). In the
public serving tier the default provider is VenueFallbackOhlcv (source:
"venue_live"); inject a PitArchiveOhlcv-wrapped provider for PIT-backed
source: "pit_archive"/"mixed" behaviour.
"""
from __future__ import annotations

import contextlib
import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from pytheum.api.ref_utils import normalize_ref
from pytheum.ohlcv.provider import OhlcvProvider
from pytheum.ohlcv.resample import _INTERVALS, resample_to_ohlcv
from pytheum.ohlcv.venue_fallback import (
    VenueFallbackOhlcv,
    _parse_kalshi_candles,
)
from pytheum.trader.cache import SingleFlightCache

logger = logging.getLogger(__name__)

__all__ = ["_parse_kalshi_candles", "handle_market_ohlcv", "resample_to_ohlcv"]

# Module-level cache shared across requests (same pattern as markets_book.py).
_module_cache = SingleFlightCache()


# ── Parameter helpers ──────────────────────────────────────────────────────────

def _parse_interval(raw: str | None) -> tuple[str, int] | None:
    """Return (label, seconds) or None for an unrecognised interval string."""
    label = (raw or "1h").lower()
    s = _INTERVALS.get(label)
    return (label, s) if s is not None else None


def _parse_ts(raw: str | None, *, default: datetime) -> datetime:
    """Parse a since/until query param (ISO-8601 or Unix-seconds) to UTC datetime."""
    if raw is None:
        return default
    # Unix-seconds integer?
    try:
        return datetime.fromtimestamp(int(raw), tz=UTC)
    except (ValueError, TypeError, OSError):
        pass
    # ISO-8601 string?
    try:
        d = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        return d.replace(tzinfo=UTC) if d.tzinfo is None else d
    except (ValueError, AttributeError):
        return default


# ── Main handler ───────────────────────────────────────────────────────────────

async def handle_market_ohlcv(
    ref: str,
    query: dict[str, str],
    *,
    provider: OhlcvProvider | None = None,
    dao: Any = None,
    clients: Any = None,
    _cache: SingleFlightCache = _module_cache,
) -> tuple[int, dict[str, Any]]:
    """GET /v1/markets/{ref}/ohlcv handler.

    Injection modes
    ---------------
    New (server.py):  pass provider=PitArchiveOhlcv(dao, VenueFallbackOhlcv(trader))
    Legacy (tests):   pass dao=..., clients=..., _cache=... — provider is built lazily

    Never raises (never-500 convention).
    """
    ref_norm = normalize_ref(ref)

    # ── Validate interval ──────────────────────────────────────────────────────
    iv_result = _parse_interval(query.get("interval"))
    if iv_result is None:
        return 200, {
            "error": "invalid_interval",
            "hint": f"interval must be one of {sorted(_INTERVALS)}. "
                    f"Got {query.get('interval')!r}.",
        }
    interval, _interval_s = iv_result  # _interval_s kept for clarity; provider uses interval

    # ── Parse limit ────────────────────────────────────────────────────────────
    try:
        limit = max(1, min(int(query.get("limit", 500)), 1000))
    except (ValueError, TypeError):
        limit = 500

    # ── Parse since / until ────────────────────────────────────────────────────
    now = datetime.now(UTC)
    since = _parse_ts(query.get("since"), default=now - timedelta(days=7))
    until = _parse_ts(query.get("until"), default=now)
    if until > now:
        until = now  # no future buckets
    if since >= until:
        return 200, {
            "error": "invalid_range",
            "hint": "`since` must be strictly before `until`.",
        }

    # ── Venue / market metadata ────────────────────────────────────────────────
    head, sep, _ = ref_norm.partition(":")
    venue = head.lower() if sep else ""

    market_meta: dict[str, Any] | None = None
    fetch_mkt = getattr(dao, "fetch_market", None)
    if fetch_mkt is not None:
        with contextlib.suppress(Exception):
            market_meta = await fetch_mkt(ref_norm)

    market_block: dict[str, Any] = {
        "id":       ref_norm,
        "question": (market_meta or {}).get("question"),
        "venue":    venue or None,
    }

    # ── Build provider lazily from legacy kwargs when not injected ─────────────
    # Public repo: no PitArchiveOhlcv. Default is VenueFallbackOhlcv (venue_live
    # source). Callers that want pit_archive source should inject a provider.
    if provider is None:
        provider = VenueFallbackOhlcv(clients, _cache)

    # ── Delegate to provider ───────────────────────────────────────────────────
    result = await provider.get_bars(ref_norm, interval, since, until, limit)

    return 200, {
        "market":   market_block,
        "interval": interval,
        "candles":  result.bars,
        "meta": {
            "source":              result.source,
            "count":               len(result.bars),
            "partial_last_bucket": result.partial_last_bucket,
        },
    }
