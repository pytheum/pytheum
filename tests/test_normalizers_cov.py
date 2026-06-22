"""Edge-case coverage tests for pytheum.trader.normalizers.

Targets the branches the happy-path tests in test_trader_data.py /
test_trader_analytics.py don't reach:
  • _safe_float — None/nan/inf/non-numeric fallbacks
  • _top_of_book — empty bids, empty asks, one-sided (no spread/mid)
  • normalize_kalshi_book — malformed levels (too short / unparseable)
  • normalize_pm_trades — non-numeric ts kept as str; None ts -> None
  • normalize_pm_whale_trades — None price/size rows skipped; non-numeric ts
  • normalize_pm_activity — type/size alt keys, non-numeric ts
  • normalize_pm_leaderboard / positions — alt-key fallbacks

Pure-function tests — no network, no clients.
"""
from __future__ import annotations

import math
from typing import Any

import pytest

from pytheum.trader.normalizers import (
    _safe_float,
    _top_of_book,
    normalize_kalshi_book,
    normalize_pm_activity,
    normalize_pm_leaderboard,
    normalize_pm_positions,
    normalize_pm_trades,
    normalize_pm_whale_trades,
)

# ─────────────────────────────────────────────────────────────────────────────
# _safe_float
# ─────────────────────────────────────────────────────────────────────────────


def test_safe_float_none_returns_fallback() -> None:
    assert _safe_float(None) is None
    assert _safe_float(None, fallback=7.0) == 7.0


def test_safe_float_nan_returns_none() -> None:
    assert _safe_float(float("nan")) is None


def test_safe_float_inf_returns_none() -> None:
    assert _safe_float(float("inf")) is None
    assert _safe_float(float("-inf")) is None


def test_safe_float_non_numeric_returns_fallback() -> None:
    assert _safe_float("not-a-number") is None
    assert _safe_float("not-a-number", fallback=-1.0) == -1.0
    assert _safe_float([1, 2, 3]) is None


def test_safe_float_parses_numeric_string() -> None:
    assert _safe_float("0.55") == pytest.approx(0.55)
    assert _safe_float(3) == 3.0


# ─────────────────────────────────────────────────────────────────────────────
# _top_of_book
# ─────────────────────────────────────────────────────────────────────────────


def test_top_of_book_empty_both_sides() -> None:
    top = _top_of_book([], [])
    assert top["bid"] is None
    assert top["bid_size"] is None
    assert top["ask"] is None
    assert top["ask_size"] is None
    assert top["spread"] is None
    assert top["mid"] is None
    assert top["mid_reliable"] is False


def test_top_of_book_only_bids_no_spread() -> None:
    top = _top_of_book([[0.40, 100.0]], [])
    assert top["bid"] == 0.40
    assert top["bid_size"] == 100.0
    assert top["ask"] is None
    # only one side present -> no spread/mid
    assert top["spread"] is None
    assert top["mid"] is None
    assert top["mid_reliable"] is False


def test_top_of_book_only_asks_no_spread() -> None:
    top = _top_of_book([], [[0.60, 50.0]])
    assert top["ask"] == 0.60
    assert top["bid"] is None
    assert top["spread"] is None
    assert top["mid"] is None


def test_top_of_book_two_sided_computes_spread_and_mid() -> None:
    top = _top_of_book([[0.40, 10.0]], [[0.42, 10.0]])
    assert top["spread"] == pytest.approx(0.02)
    assert top["mid"] == pytest.approx(0.41)
    assert top["mid_reliable"] is True


# ─────────────────────────────────────────────────────────────────────────────
# normalize_kalshi_book — malformed levels
# ─────────────────────────────────────────────────────────────────────────────


def test_kalshi_book_skips_short_and_unparseable_levels() -> None:
    body = {
        "orderbook_fp": {
            "yes_dollars": [
                ["0.55", "100"],   # valid
                ["0.54"],          # too short -> skipped
                [],                # empty -> skipped
                ["abc", "xyz"],    # unparseable -> skipped
            ],
            "no_dollars": [
                ["0.43", "150"],   # valid
                [None, None],      # None price/size -> skipped
            ],
        }
    }
    result = normalize_kalshi_book(body, ref="kalshi:KX", depth=20)
    # only the single valid level on each side survives
    assert len(result["bids"]) == 1
    assert result["bids"][0][0] == 0.55
    assert len(result["asks"]) == 1
    # implied ask = 1 - 0.43
    assert result["asks"][0][0] == pytest.approx(0.57)


def test_kalshi_book_empty_orderbook_yields_empty_sides() -> None:
    result = normalize_kalshi_book({}, ref="kalshi:KX", depth=20)
    assert result["bids"] == []
    assert result["asks"] == []
    assert result["top"]["bid"] is None


def test_kalshi_book_alt_keys_yes_no() -> None:
    """Fallback to plain `orderbook`/`yes`/`no` keys (not _fp/_dollars)."""
    body = {"orderbook": {"yes": [["0.50", "10"]], "no": [["0.45", "20"]]}}
    result = normalize_kalshi_book(body, ref="kalshi:KX", depth=20)
    assert result["bids"][0][0] == 0.50
    assert result["asks"][0][0] == pytest.approx(0.55)


# ─────────────────────────────────────────────────────────────────────────────
# normalize_pm_trades — ts edge cases
# ─────────────────────────────────────────────────────────────────────────────


def test_pm_trades_non_numeric_ts_kept_as_string() -> None:
    items = [{"price": "0.5", "size": "10", "side": "BUY", "timestamp": "2026-01-01T00:00:00Z"}]
    out = normalize_pm_trades(items, limit=10)
    assert out[0]["ts"] == "2026-01-01T00:00:00Z"


def test_pm_trades_none_ts_is_none() -> None:
    items = [{"price": "0.5", "size": "10", "side": "BUY", "timestamp": None}]
    out = normalize_pm_trades(items, limit=10)
    assert out[0]["ts"] is None


def test_pm_trades_seconds_epoch_not_divided() -> None:
    # value below 1e12 is treated as seconds (not ms)
    items = [{"price": "0.5", "size": "10", "side": "BUY", "timestamp": 1700000000}]
    out = normalize_pm_trades(items, limit=10)
    assert "1970" not in out[0]["ts"]  # sane recent year, not ms-as-seconds


def test_pm_trades_invalid_side_becomes_none() -> None:
    items = [{"price": "0.5", "size": "10", "side": "WEIRD", "timestamp": 1700000000}]
    out = normalize_pm_trades(items, limit=10)
    assert out[0]["side"] is None


def test_pm_trades_none_price_size_round_skipped() -> None:
    items = [{"price": None, "size": None, "side": "BUY", "timestamp": 1700000000}]
    out = normalize_pm_trades(items, limit=10)
    assert out[0]["price"] is None
    assert out[0]["size"] is None


# ─────────────────────────────────────────────────────────────────────────────
# normalize_pm_whale_trades — skip + ts edge cases
# ─────────────────────────────────────────────────────────────────────────────


def test_whale_trades_skips_rows_missing_price_or_size() -> None:
    items: list[dict[str, Any]] = [
        {"price": None, "size": "100", "timestamp": 1700000000},   # no price -> skip
        {"price": "0.8", "size": None, "timestamp": 1700000000},   # no size  -> skip
        {"price": "0.8", "size": "1000", "timestamp": 1700000000, "maker": "0xw"},  # ok
    ]
    out = normalize_pm_whale_trades(items, min_usd=1.0, limit=10, ref=None)
    assert len(out) == 1
    assert out[0]["wallet"] == "0xw"


def test_whale_trades_ms_timestamp_converted() -> None:
    items = [{"price": "0.8", "size": "1000", "timestamp": 1_700_000_000_000}]
    out = normalize_pm_whale_trades(items, min_usd=1.0, limit=10, ref=None)
    assert "T" in out[0]["ts"]


def test_whale_trades_string_ts_kept() -> None:
    items = [{"price": "0.8", "size": "1000", "timestamp": "2026-01-01T00:00:00Z"}]
    out = normalize_pm_whale_trades(items, min_usd=1.0, limit=10, ref=None)
    assert out[0]["ts"] == "2026-01-01T00:00:00Z"


def test_whale_trades_none_ts_is_none() -> None:
    items = [{"price": "0.8", "size": "1000", "timestamp": None}]
    out = normalize_pm_whale_trades(items, min_usd=1.0, limit=10, ref=None)
    assert out[0]["ts"] is None


def test_whale_trades_invalid_side_none() -> None:
    items = [{"price": "0.8", "size": "1000", "side": "nope", "timestamp": 1700000000}]
    out = normalize_pm_whale_trades(items, min_usd=1.0, limit=10, ref=None)
    assert out[0]["side"] is None


# ─────────────────────────────────────────────────────────────────────────────
# normalize_pm_activity — alt keys + ts edges
# ─────────────────────────────────────────────────────────────────────────────


def test_pm_activity_type_and_amount_alt_keys() -> None:
    # 'type' used instead of 'side'; 'amount' instead of 'size'
    items = [{"market": "0x1", "outcome": "YES", "price": "0.5",
              "amount": "100", "type": "SELL", "ts": 1700000000}]
    out = normalize_pm_activity(items)
    assert out[0]["side"] == "SELL"
    assert out[0]["size"] == 100.0


def test_pm_activity_ms_ts_converted_and_string_ts_kept() -> None:
    items: list[dict[str, Any]] = [
        {"market": "0x1", "price": "0.5", "size": "1", "side": "BUY",
         "timestamp": 1_700_000_000_000},
        {"market": "0x2", "price": "0.5", "size": "1", "side": "BUY",
         "timestamp": "raw-string"},
        {"market": "0x3", "price": "0.5", "size": "1", "side": "BUY",
         "timestamp": None},
    ]
    out = normalize_pm_activity(items)
    assert "T" in out[0]["ts"]
    assert out[1]["ts"] == "raw-string"
    assert out[2]["ts"] is None


# ─────────────────────────────────────────────────────────────────────────────
# normalize_pm_leaderboard / positions — alt-key fallbacks
# ─────────────────────────────────────────────────────────────────────────────


def test_leaderboard_alt_keys_pnl_proxywallet_ranking() -> None:
    items = [{"pseudonym": "Foxy", "proxyWallet": "0xp", "pnl": "12.5",
              "positions_value": "99.0", "ranking": 3}]
    out = normalize_pm_leaderboard(items, period="weekly")
    t0 = out["traders"][0]
    assert t0["name"] == "Foxy"
    assert t0["address"] == "0xp"
    assert t0["profit"] == pytest.approx(12.5)
    assert t0["positions_value"] == pytest.approx(99.0)
    assert t0["rank"] == 3


def test_positions_alt_keys_average_price_and_value() -> None:
    items = [{"conditionId": "0xc", "outcome": "NO", "amount": "5",
              "averagePrice": "0.3", "value": "1.5", "pnl": "0.2"}]
    out = normalize_pm_positions(items)
    p = out[0]
    assert p["market"] == "0xc"
    assert p["size"] == 5.0
    assert p["avg_price"] == pytest.approx(0.3)
    assert p["current_value"] == pytest.approx(1.5)
    assert p["profit"] == pytest.approx(0.2)


def test_safe_float_used_in_round_paths() -> None:
    # sanity: a clearly-finite parse round-trips through math correctly
    assert not math.isnan(_safe_float("0.123") or 0.0)
