"""Tests for pytheum.ohlcv.resample — the canonical module location.

These tests import resample_to_ohlcv directly from its new home in
ohlcv/resample.py (Stage 2 extraction).  The corresponding tests in
tests/unit/test_ohlcv.py exercise the same function via the re-export
in api/markets_ohlcv, ensuring backwards compatibility.
"""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from pytheum.ohlcv.resample import _INTERVALS, resample_to_ohlcv

# ── Constants ──────────────────────────────────────────────────────────────────

_1H = 3600
_5M = 300
_1D = 86400


def _pts(*items: tuple[datetime, float, int | None]) -> list[dict[str, Any]]:
    return [{"ts": ts, "yes_price": p, "n_trades": n} for ts, p, n in items]


# ── Interval registry ──────────────────────────────────────────────────────────

def test_intervals_contain_standard_set() -> None:
    assert set(_INTERVALS) == {"1m", "5m", "15m", "1h", "1d"}
    assert _INTERVALS["1h"] == 3600
    assert _INTERVALS["1d"] == 86400


# ── resample_to_ohlcv — imported from canonical location ──────────────────────

def test_single_point_produces_correct_ohlcv() -> None:
    since = datetime(2026, 6, 1, 0, 0, tzinfo=UTC)
    until = datetime(2026, 6, 1, 2, 0, tzinfo=UTC)
    pts = _pts((datetime(2026, 6, 1, 0, 30, tzinfo=UTC), 0.55, 10))
    candles, partial = resample_to_ohlcv(pts, _1H, since=since, until=until, limit=500)
    assert len(candles) == 1
    c = candles[0]
    assert c["t"] == "2026-06-01T00:00:00Z"
    assert c["o"] == c["h"] == c["l"] == c["c"] == 0.55
    assert c["v"] == 10
    assert partial is False


def test_multiple_points_in_bucket_aggregated() -> None:
    since = datetime(2026, 6, 1, 0, 0, tzinfo=UTC)
    until = datetime(2026, 6, 1, 2, 0, tzinfo=UTC)
    pts = _pts(
        (datetime(2026, 6, 1, 0, 10, tzinfo=UTC), 0.50, 5),
        (datetime(2026, 6, 1, 0, 20, tzinfo=UTC), 0.70, 3),
        (datetime(2026, 6, 1, 0, 40, tzinfo=UTC), 0.45, 2),
        (datetime(2026, 6, 1, 0, 55, tzinfo=UTC), 0.60, 8),
    )
    candles, _ = resample_to_ohlcv(pts, _1H, since=since, until=until, limit=500)
    assert len(candles) == 1
    c = candles[0]
    assert c["o"] == 0.50
    assert c["h"] == 0.70
    assert c["l"] == 0.45
    assert c["c"] == 0.60
    assert c["v"] == 18


def test_multiple_buckets_ascending_order() -> None:
    since = datetime(2026, 6, 1, 0, 0, tzinfo=UTC)
    until = datetime(2026, 6, 1, 3, 0, tzinfo=UTC)
    pts = _pts(
        (datetime(2026, 6, 1, 0, 30, tzinfo=UTC), 0.40, 1),
        (datetime(2026, 6, 1, 1, 30, tzinfo=UTC), 0.50, 2),
        (datetime(2026, 6, 1, 2, 30, tzinfo=UTC), 0.60, 3),
    )
    candles, _ = resample_to_ohlcv(pts, _1H, since=since, until=until, limit=500)
    assert len(candles) == 3
    assert candles[0]["t"] == "2026-06-01T00:00:00Z"
    assert candles[1]["t"] == "2026-06-01T01:00:00Z"
    assert candles[2]["t"] == "2026-06-01T02:00:00Z"


def test_empty_input_returns_empty() -> None:
    since = datetime(2026, 6, 1, tzinfo=UTC)
    until = datetime(2026, 6, 2, tzinfo=UTC)
    candles, partial = resample_to_ohlcv([], _1H, since=since, until=until, limit=500)
    assert candles == []
    assert partial is False


def test_points_outside_range_excluded() -> None:
    since = datetime(2026, 6, 1, 10, 0, tzinfo=UTC)
    until = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)
    pts = _pts(
        (datetime(2026, 6, 1, 9, 0, tzinfo=UTC),  0.40, 1),   # before since
        (datetime(2026, 6, 1, 11, 0, tzinfo=UTC), 0.55, 2),   # in range
        (datetime(2026, 6, 1, 12, 0, tzinfo=UTC), 0.70, 3),   # at until — excluded
    )
    candles, _ = resample_to_ohlcv(pts, _1H, since=since, until=until, limit=500)
    assert len(candles) == 1
    assert candles[0]["c"] == 0.55


def test_volume_none_when_all_n_trades_none() -> None:
    since = datetime(2026, 6, 1, tzinfo=UTC)
    until = datetime(2026, 6, 2, tzinfo=UTC)
    pts = [{"ts": datetime(2026, 6, 1, 1, tzinfo=UTC), "yes_price": 0.5, "n_trades": None}]
    candles, _ = resample_to_ohlcv(pts, _1H, since=since, until=until, limit=500)
    assert candles[0]["v"] is None


def test_limit_clips_oldest() -> None:
    since = datetime(2026, 6, 1, tzinfo=UTC)
    until = datetime(2026, 6, 1, 10, tzinfo=UTC)
    pts = _pts(*[(datetime(2026, 6, 1, h, 30, tzinfo=UTC), 0.5, 1) for h in range(10)])
    candles, _ = resample_to_ohlcv(pts, _1H, since=since, until=until, limit=3)
    assert len(candles) == 3
    assert candles[-1]["t"].startswith("2026-06-01T09:")


def test_5m_bucket_boundaries() -> None:
    since = datetime(2026, 6, 1, tzinfo=UTC)
    until = datetime(2026, 6, 1, 0, 10, tzinfo=UTC)
    pts = _pts(
        (datetime(2026, 6, 1, 0, 2, tzinfo=UTC), 0.40, 2),   # bucket 00:00
        (datetime(2026, 6, 1, 0, 7, tzinfo=UTC), 0.60, 3),   # bucket 00:05
    )
    candles, _ = resample_to_ohlcv(pts, _5M, since=since, until=until, limit=500)
    assert len(candles) == 2
    assert candles[0]["t"] == "2026-06-01T00:00:00Z"
    assert candles[1]["t"] == "2026-06-01T00:05:00Z"


def test_partial_flag_is_bool() -> None:
    """partial_last_bucket is always a bool (regression guard)."""
    since = datetime(2026, 6, 1, 0, 0, tzinfo=UTC)
    until = datetime(2026, 6, 1, 2, 0, tzinfo=UTC)
    pts = _pts((datetime(2026, 6, 1, 0, 30, tzinfo=UTC), 0.55, 1))
    _, partial = resample_to_ohlcv(pts, _1H, since=since, until=until, limit=500)
    assert isinstance(partial, bool)
