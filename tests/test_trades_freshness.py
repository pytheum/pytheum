"""Unit tests for the trade-tape freshness helpers in markets_trades.

These pin the honest-freshness signal: a settled market whose last trades fired
at resolution and never refreshed must read as stale even though source=="live".
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

from pytheum.api.markets_trades import (
    _TRADE_STALE_GRACE_S,
    _parse_ts_epoch,
    _trade_freshness,
)


def _iso(dt: datetime) -> str:
    return dt.isoformat().replace("+00:00", "Z")


def test_parse_ts_epoch_iso_and_epoch():
    now = datetime.now(UTC)
    assert abs(_parse_ts_epoch(_iso(now)) - now.timestamp()) < 1.0
    # naive ISO is treated as UTC, not rejected
    assert _parse_ts_epoch(now.replace(tzinfo=None).isoformat()) is not None
    # epoch seconds and milliseconds both normalise to seconds
    assert _parse_ts_epoch(1_700_000_000) == 1_700_000_000.0
    assert _parse_ts_epoch(1_700_000_000_000) == 1_700_000_000.0
    assert _parse_ts_epoch("1700000000") == 1_700_000_000.0
    # junk degrades to None, never raises
    assert _parse_ts_epoch(None) is None
    assert _parse_ts_epoch("") is None
    assert _parse_ts_epoch("not-a-date") is None


def test_trade_freshness_live_is_not_stale():
    now = datetime.now(UTC)
    age, stale = _trade_freshness([{"ts": _iso(now - timedelta(minutes=5))}])
    assert age is not None and age < 3600
    assert stale is False


def test_trade_freshness_settled_tape_is_stale():
    now = datetime.now(UTC)
    age, stale = _trade_freshness([{"ts": _iso(now - timedelta(hours=25))}])
    assert age > _TRADE_STALE_GRACE_S
    assert stale is True


def test_trade_freshness_uses_newest_trade():
    now = datetime.now(UTC)
    trades = [
        {"ts": _iso(now - timedelta(hours=25))},  # old
        {"ts": _iso(now - timedelta(minutes=2))},  # newest → governs freshness
    ]
    age, stale = _trade_freshness(trades)
    assert stale is False
    assert age < 3600


def test_trade_freshness_empty_or_unparseable():
    assert _trade_freshness([]) == (None, False)
    assert _trade_freshness([{"ts": None}, {"ts": "x"}]) == (None, False)
