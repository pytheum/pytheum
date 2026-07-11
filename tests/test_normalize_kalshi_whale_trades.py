"""Unit tests for normalize_kalshi_whale_trades.

Covers: sub-threshold filtering, descending sort by notional, limit cap, the
anonymity guarantee (no "wallet" key), taker_side yes→BUY / no→SELL price-leg
selection, skipping rows with missing price/size, the empty case, and the
"kalshi:<ticker>" market id.
"""
from __future__ import annotations

from typing import Any

from pytheum.trader.normalizers import normalize_kalshi_whale_trades


def _trade(
    *,
    taker_side: str = "yes",
    yes_price: str | None = "0.50",
    no_price: str | None = "0.50",
    count: Any = "100",
    ticker: str | None = "MKT-A",
    created_time: str | None = "2026-06-28T00:00:00Z",
) -> dict[str, Any]:
    """Build a synthetic Kalshi-shaped trade dict."""
    item: dict[str, Any] = {
        "taker_side": taker_side,
        "count_fp": count,
    }
    if yes_price is not None:
        item["yes_price_dollars"] = yes_price
    if no_price is not None:
        item["no_price_dollars"] = no_price
    if ticker is not None:
        item["ticker"] = ticker
    if created_time is not None:
        item["created_time"] = created_time
    return item


def test_filters_out_sub_threshold_notional() -> None:
    # notional = 0.50 * 100 = 50.0 → below min_usd=100 → dropped.
    body = {"trades": [_trade(yes_price="0.50", count="100")]}
    out = normalize_kalshi_whale_trades(body, min_usd=100.0, limit=10)
    assert out == []


def test_keeps_at_threshold_notional() -> None:
    # notional exactly 50.0 with min_usd=50.0 → kept (>=).
    body = {"trades": [_trade(yes_price="0.50", count="100")]}
    out = normalize_kalshi_whale_trades(body, min_usd=50.0, limit=10)
    assert len(out) == 1
    assert out[0]["notional_usd"] == 50.0


def test_sorts_descending_by_notional() -> None:
    body = {
        "trades": [
            _trade(yes_price="0.10", count="100", ticker="SMALL"),   # 10
            _trade(yes_price="0.90", count="1000", ticker="BIG"),    # 900
            _trade(yes_price="0.50", count="200", ticker="MID"),     # 100
        ]
    }
    out = normalize_kalshi_whale_trades(body, min_usd=0.0, limit=10)
    notionals = [t["notional_usd"] for t in out]
    assert notionals == sorted(notionals, reverse=True)
    assert [t["market"] for t in out] == ["kalshi:BIG", "kalshi:MID", "kalshi:SMALL"]


def test_honors_limit() -> None:
    body = {
        "trades": [
            _trade(yes_price="0.90", count="1000", ticker="A"),  # 900
            _trade(yes_price="0.80", count="1000", ticker="B"),  # 800
            _trade(yes_price="0.70", count="1000", ticker="C"),  # 700
        ]
    }
    out = normalize_kalshi_whale_trades(body, min_usd=0.0, limit=2)
    assert len(out) == 2
    # Top 2 by notional.
    assert [t["market"] for t in out] == ["kalshi:A", "kalshi:B"]


def test_no_wallet_key_present() -> None:
    body = {"trades": [_trade(yes_price="0.90", count="1000")]}
    out = normalize_kalshi_whale_trades(body, min_usd=0.0, limit=10)
    assert len(out) == 1
    assert "wallet" not in out[0]
    # Exact key set — anonymity is the whole point.
    assert set(out[0].keys()) == {"ts", "market", "price", "size", "notional_usd", "side"}


def test_taker_side_yes_is_buy_uses_yes_price() -> None:
    body = {
        "trades": [
            _trade(taker_side="yes", yes_price="0.30", no_price="0.70", count="1000"),
        ]
    }
    out = normalize_kalshi_whale_trades(body, min_usd=0.0, limit=10)
    assert out[0]["side"] == "BUY"
    assert out[0]["price"] == 0.30
    assert out[0]["notional_usd"] == 300.0


def test_taker_side_no_is_sell_uses_no_price() -> None:
    body = {
        "trades": [
            _trade(taker_side="no", yes_price="0.30", no_price="0.70", count="1000"),
        ]
    }
    out = normalize_kalshi_whale_trades(body, min_usd=0.0, limit=10)
    assert out[0]["side"] == "SELL"
    assert out[0]["price"] == 0.70
    assert out[0]["notional_usd"] == 700.0


def test_skips_missing_price() -> None:
    # taker_side yes but no yes_price_dollars → price None → skipped.
    body = {"trades": [_trade(taker_side="yes", yes_price=None, count="1000")]}
    out = normalize_kalshi_whale_trades(body, min_usd=0.0, limit=10)
    assert out == []


def test_skips_missing_size() -> None:
    item = _trade(yes_price="0.90")
    item.pop("count_fp", None)  # no count_fp / count → size None → skipped.
    body = {"trades": [item]}
    out = normalize_kalshi_whale_trades(body, min_usd=0.0, limit=10)
    assert out == []


def test_empty_trades_returns_empty() -> None:
    assert normalize_kalshi_whale_trades({"trades": []}, min_usd=0.0, limit=10) == []


def test_missing_trades_key_returns_empty() -> None:
    assert normalize_kalshi_whale_trades({}, min_usd=0.0, limit=10) == []


def test_market_is_kalshi_ticker_when_present() -> None:
    body = {"trades": [_trade(yes_price="0.90", count="1000", ticker="FED-2026")]}
    out = normalize_kalshi_whale_trades(body, min_usd=0.0, limit=10)
    assert out[0]["market"] == "kalshi:FED-2026"


def test_market_none_when_ticker_absent() -> None:
    body = {"trades": [_trade(yes_price="0.90", count="1000", ticker=None)]}
    out = normalize_kalshi_whale_trades(body, min_usd=0.0, limit=10)
    assert out[0]["market"] is None


def test_falls_back_to_count_and_trade_date() -> None:
    item = {
        "taker_side": "yes",
        "yes_price_dollars": "0.50",
        "count": "400",  # no count_fp
        "ticker": "X",
        "trade_date": "2026-06-28",  # no created_time
    }
    out = normalize_kalshi_whale_trades({"trades": [item]}, min_usd=0.0, limit=10)
    assert out[0]["size"] == 400.0
    assert out[0]["notional_usd"] == 200.0
    assert out[0]["ts"] == "2026-06-28"
