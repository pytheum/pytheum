"""OHLCV resampling: price change-points → candle buckets.

Canonical home of resample_to_ohlcv (moved from api/markets_ohlcv in Stage 2).
api/markets_ohlcv re-exports it for backwards compatibility.
"""
from __future__ import annotations

import math
from datetime import UTC, datetime
from typing import Any

__all__ = ["_INTERVALS", "_KALSHI_NATIVE", "resample_to_ohlcv"]

# Supported intervals: label → seconds.
_INTERVALS: dict[str, int] = {
    "1m":  60,
    "5m":  300,
    "15m": 900,
    "1h":  3600,
    "1d":  86400,
}

# Kalshi native intervals (5m/15m must be aggregated from 1m client-side).
_KALSHI_NATIVE: frozenset[str] = frozenset({"1m", "1h", "1d"})


def _bucket_start(ts: datetime, interval_s: int) -> datetime:
    """Floor *ts* to the bucket's UTC start instant."""
    return datetime.fromtimestamp(
        math.floor(ts.timestamp() / interval_s) * interval_s, tz=UTC
    )


def resample_to_ohlcv(
    points: list[dict[str, Any]],
    interval_s: int,
    *,
    since: datetime,
    until: datetime,
    limit: int,
) -> tuple[list[dict[str, Any]], bool]:
    """Resample price change-points into OHLCV candles.

    Each input point must be:
        {"ts": datetime (UTC), "yes_price": float, "n_trades": int | None}

    Returns (candles, partial_last_bucket) where:
    • candles:  list of {"t": ISO-Z str (bucket START), "o", "h", "l", "c", "v"}
                ascending by bucket start, clipped to `limit` newest.
    • partial_last_bucket: True when the last emitted bucket's END time exceeds
                the current wall-clock — the bucket is still open/incomplete.

    Bucketing contract:
    - Points outside [since, until) are excluded first (no lookahead).
    - Empty buckets emit no candle row (no forward-fill).
    - Volume v = sum of n_trades for all points in the bucket; None when all
      n_trades values are None (e.g. live-tape rows that carry no trade count).
    - When len > limit the oldest candles are dropped (keep newest).
    """
    now = datetime.now(UTC)

    # Exclude any point outside [since, until).
    pts = [p for p in points if since <= p["ts"] < until]
    if not pts:
        return [], False

    # Group by bucket-start epoch (float key for stable sorting).
    buckets: dict[float, list[dict[str, Any]]] = {}
    for p in pts:
        key = _bucket_start(p["ts"], interval_s).timestamp()
        buckets.setdefault(key, []).append(p)

    candles: list[dict[str, Any]] = []
    for bkey in sorted(buckets):
        bpts = sorted(buckets[bkey], key=lambda p: p["ts"])
        prices = [float(p["yes_price"]) for p in bpts]
        raw_trades = [p.get("n_trades") for p in bpts]
        have_volume = any(t is not None for t in raw_trades)
        v: int | None = sum(int(t or 0) for t in raw_trades) if have_volume else None

        bs = datetime.fromtimestamp(bkey, tz=UTC)
        candles.append({
            "t": bs.isoformat().replace("+00:00", "Z"),
            "o": round(prices[0],   4),
            "h": round(max(prices), 4),
            "l": round(min(prices), 4),
            "c": round(prices[-1],  4),
            "v": v,
        })

    # Partial-bucket flag: last bucket's end time exceeds now.
    partial = False
    if candles:
        last_bs_epoch = _bucket_start(
            datetime.fromisoformat(candles[-1]["t"].replace("Z", "+00:00")),
            interval_s,
        ).timestamp()
        partial = (last_bs_epoch + interval_s) > now.timestamp()

    # Clip to `limit` newest.
    if len(candles) > limit:
        candles = candles[-limit:]

    return candles, partial
