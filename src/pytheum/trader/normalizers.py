"""Normalize raw venue responses to the common trader-data schema.

Kalshi wire format notes (DEVIATION from original plan — see kalshi/normalizer.py):
  • Orderbook:  outer key ``orderbook_fp``, sides ``yes_dollars`` / ``no_dollars``,
    levels as [price_dollars_str, size_fp_str] in [0, 1] range (already in prob
    units, no cents conversion needed).
  • Trades:     fields ``count_fp`` (size), ``yes_price_dollars`` / ``no_price_dollars``
    (string dollars), ``taker_side`` ("yes"/"no").
  • OI:         market body field ``open_interest`` (int or string).

Polymarket CLOB wire format:
  • Orderbook:  ``{"bids": [{"price": str, "size": str}, ...], "asks": [...]}``
  • Trades (Data API): list of dicts with ``price``, ``size``, ``side``, ``timestamp``.
  • OI (Data API): list of ``{"asset_id": str, "market": str, "open_interest_count": str}``.

Output schema (common across venues):
  • book:   {bids: [[price, size], ...], asks: [...], venue, ts, source, top}
  • trades: [{ts, price, size, side}, ...]
  • oi:     {open_interest, venue, ref, source}
"""
from __future__ import annotations

import math
from datetime import UTC, datetime
from typing import Any

__all__ = [
    "normalize_kalshi_book",
    "normalize_pm_book",
    "normalize_kalshi_trades",
    "normalize_pm_trades",
    "normalize_kalshi_oi",
    "normalize_pm_oi",
    # trader analytics (P1)
    "normalize_pm_leaderboard",
    "normalize_pm_holders",
    "normalize_pm_positions",
    "normalize_pm_activity",
    "normalize_pm_value",
    "normalize_pm_whale_trades",
]


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _safe_float(v: Any, fallback: float | None = None) -> float | None:
    if v is None:
        return fallback
    try:
        f = float(v)
        return None if math.isnan(f) or math.isinf(f) else f
    except (TypeError, ValueError):
        return fallback


def _top_of_book(
    bids: list[list[float]], asks: list[list[float]]
) -> dict[str, float | None]:
    """Compute top-of-book summary (best bid/ask/spread/mid + sizes)."""
    top: dict[str, Any] = {}
    # bids sorted desc, asks sorted asc
    if bids:
        top["bid"] = bids[0][0]
        top["bid_size"] = bids[0][1]
    else:
        top["bid"] = None
        top["bid_size"] = None
    if asks:
        top["ask"] = asks[0][0]
        top["ask_size"] = asks[0][1]
    else:
        top["ask"] = None
        top["ask_size"] = None
    bid_v = top["bid"]
    ask_v = top["ask"]
    if bid_v is not None and ask_v is not None:
        top["spread"] = round(ask_v - bid_v, 6)
        top["mid"] = round((bid_v + ask_v) / 2, 6)
    else:
        top["spread"] = None
        top["mid"] = None
    return top


# ── Kalshi ─────────────────────────────────────────────────────────────────

def normalize_kalshi_book(
    body: dict[str, Any],
    *,
    ref: str,
    depth: int,
) -> dict[str, Any]:
    """Normalize ``GET /markets/{ticker}/orderbook`` (orderbook_fp format)."""
    # Kalshi returns outer key "orderbook_fp" with dollar-encoded string levels.
    # Each level is [price_dollars_str, size_fp_str]; prices already in [0,1].
    # YES bids = buy YES orders; NO bids = buy NO orders.
    # YES asks = implied by NO bids: price = 1 - no_price.
    ob = body.get("orderbook_fp") or body.get("orderbook") or {}
    yes_raw: list[list[Any]] = ob.get("yes_dollars") or ob.get("yes") or []
    no_raw: list[list[Any]] = ob.get("no_dollars") or ob.get("no") or []

    def _parse_level(lvl: list[Any]) -> list[float] | None:
        if not lvl or len(lvl) < 2:
            return None
        p = _safe_float(lvl[0])
        s = _safe_float(lvl[1])
        if p is None or s is None:
            return None
        return [round(p, 6), round(s, 4)]

    bids: list[list[float]] = []
    for lvl in yes_raw[:depth]:
        parsed = _parse_level(lvl)
        if parsed:
            bids.append(parsed)
    bids.sort(key=lambda x: x[0], reverse=True)

    asks: list[list[float]] = []
    for lvl in no_raw[:depth]:
        parsed = _parse_level(lvl)
        if parsed:
            # Implied ask = 1 - no_bid_price
            asks.append([round(1.0 - parsed[0], 6), parsed[1]])
    asks.sort(key=lambda x: x[0])

    return {
        "bids": bids,
        "asks": asks,
        "venue": "kalshi",
        "ref": ref,
        "ts": _now_iso(),
        "source": "live",
        "top": _top_of_book(bids, asks),
    }


def normalize_kalshi_trades(
    body: dict[str, Any],
    *,
    limit: int,
) -> list[dict[str, Any]]:
    """Normalize ``GET /markets/trades`` (count_fp / *_price_dollars format)."""
    raw: list[dict[str, Any]] = body.get("trades") or []
    result: list[dict[str, Any]] = []
    for item in raw[:limit]:
        taker_side = str(item.get("taker_side") or "yes").lower()
        if taker_side == "yes":
            price = _safe_float(item.get("yes_price_dollars"))
        else:
            price = _safe_float(item.get("no_price_dollars"))
        size = _safe_float(item.get("count_fp") or item.get("count"))
        ts = item.get("created_time") or item.get("trade_date")
        result.append({
            "ts": str(ts) if ts is not None else None,
            "price": round(price, 6) if price is not None else None,
            "size": round(size, 4) if size is not None else None,
            "side": "BUY" if taker_side == "yes" else "SELL",
        })
    return result


def normalize_kalshi_oi(
    body: dict[str, Any],
    *,
    ref: str,
) -> dict[str, Any]:
    """Normalize the market body from ``GET /markets/{ticker}`` for OI."""
    market = body.get("market") or body
    # Kalshi's list + detail endpoints null the plain `open_interest` field and
    # carry the real value in `open_interest_fp` (a fixed-point STRING, in
    # contracts — matches pytheum-core's ws_normalizer, which does
    # Decimal(open_interest_fp) with no scaling). Fall back to it when the plain
    # field is null.
    oi = _safe_float(market.get("open_interest"))
    if oi is None:
        oi = _safe_float(market.get("open_interest_fp"))
    return {
        "open_interest": oi,
        "venue": "kalshi",
        "ref": ref,
        "source": "live",
    }


# ── Polymarket ──────────────────────────────────────────────────────────────

def normalize_pm_book(
    body: dict[str, Any],
    *,
    ref: str,
    depth: int,
) -> dict[str, Any]:
    """Normalize CLOB ``GET /book?token_id=…`` response."""
    raw_bids: list[dict[str, Any]] = body.get("bids") or []
    raw_asks: list[dict[str, Any]] = body.get("asks") or []

    def _parse(entries: list[dict[str, Any]], n: int) -> list[list[float]]:
        out: list[list[float]] = []
        for e in entries[:n]:
            p = _safe_float(e.get("price"))
            s = _safe_float(e.get("size"))
            if p is not None and s is not None:
                out.append([round(p, 6), round(s, 4)])
        return out

    bids = _parse(raw_bids, depth)
    bids.sort(key=lambda x: x[0], reverse=True)
    asks = _parse(raw_asks, depth)
    asks.sort(key=lambda x: x[0])

    return {
        "bids": bids,
        "asks": asks,
        "venue": "polymarket",
        "ref": ref,
        "ts": _now_iso(),
        "source": "live",
        "top": _top_of_book(bids, asks),
    }


def normalize_pm_trades(
    items: list[dict[str, Any]],
    *,
    limit: int,
) -> list[dict[str, Any]]:
    """Normalize Polymarket Data API trades list."""
    result: list[dict[str, Any]] = []
    for item in items[:limit]:
        price = _safe_float(item.get("price"))
        size = _safe_float(item.get("size"))
        side_raw = str(item.get("side") or "").upper()
        side = side_raw if side_raw in ("BUY", "SELL") else None
        # timestamp may be ms or seconds epoch
        ts_raw = item.get("timestamp")
        if isinstance(ts_raw, (int, float)) and ts_raw > 1e12:
            ts_raw = ts_raw / 1000  # convert ms to seconds
        ts_str = (
            datetime.fromtimestamp(ts_raw, tz=UTC).isoformat()
            if isinstance(ts_raw, (int, float))
            else str(ts_raw) if ts_raw is not None else None
        )
        result.append({
            "ts": ts_str,
            "price": round(price, 6) if price is not None else None,
            "size": round(size, 4) if size is not None else None,
            "side": side,
        })
    return result


def normalize_pm_oi(
    items: list[dict[str, Any]],
    *,
    ref: str,
) -> dict[str, Any]:
    """Normalize Polymarket Data API open-interest list."""
    # The list is per-token; sum or take first non-null value.
    total: float | None = None
    for item in items:
        oi = _safe_float(item.get("open_interest_count") or item.get("open_interest") or item.get("value"))
        if oi is not None:
            total = (total or 0.0) + oi
    return {
        "open_interest": total,
        "venue": "polymarket",
        "ref": ref,
        "source": "live",
    }


# ── Polymarket Trader Analytics (P1) ────────────────────────────────────────

_PM_ONLY_NOTE = "Polymarket-only. Kalshi trades are anonymized."


def normalize_pm_leaderboard(
    items: list[dict[str, Any]],
    *,
    period: str,
) -> dict[str, Any]:
    """Normalize Polymarket /v1/leaderboard response."""
    traders: list[dict[str, Any]] = []
    for item in items:
        profit_raw = item.get("profit") if item.get("profit") is not None else item.get("pnl")
        addr_raw = item.get("address") if item.get("address") is not None else item.get("proxyWallet")
        traders.append({
            "name": item.get("name") or item.get("pseudonym") or item.get("username"),
            "address": addr_raw,
            "profit": _safe_float(profit_raw),
            "volume": _safe_float(item.get("volume")),
            "positions_value": _safe_float(item.get("positionsValue") or item.get("positions_value")),
            "rank": item.get("rank") or item.get("ranking"),
        })
    return {
        "period": period,
        "traders": traders,
        "count": len(traders),
        "source": "live",
        "venue": "polymarket",
        "note": _PM_ONLY_NOTE,
    }


def normalize_pm_holders(
    items: list[dict[str, Any]],
    *,
    ref: str,
) -> dict[str, Any]:
    """Normalize Polymarket /holders response.

    Venue returns two-level nesting:
      [{"token": <token_id>, "holders": [{"proxyWallet", "amount", "asset", ...}]}]
    Each outer entry wraps one token's holder list; inner records carry the real fields.
    """
    holders: list[dict[str, Any]] = []
    for outer in items:
        # Venue shape: {token: <token_id>, holders: [...per-holder-dicts...]}
        token_id = outer.get("token")
        for item in (outer.get("holders") or []):
            addr_raw = item.get("proxyWallet") or item.get("address")
            holders.append({
                "address": addr_raw,
                "amount": _safe_float(item.get("amount") or item.get("size")),
                "outcome": (
                    item.get("outcome")
                    or item.get("asset")
                    or item.get("asset_id")
                    or token_id
                ),
            })
    return {
        "holders": holders,
        "count": len(holders),
        "ref": ref,
        "source": "live",
        "venue": "polymarket",
        "note": _PM_ONLY_NOTE,
    }


def normalize_pm_positions(
    items: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Normalize Polymarket /positions response."""
    result: list[dict[str, Any]] = []
    for item in items:
        avg_raw = item.get("avgPrice") if item.get("avgPrice") is not None else item.get("averagePrice")
        val_raw = item.get("currentValue") if item.get("currentValue") is not None else item.get("value")
        pnl_raw = item.get("profit") if item.get("profit") is not None else item.get("pnl")
        result.append({
            "market": item.get("market") or item.get("conditionId"),
            "outcome": item.get("outcome"),
            "size": _safe_float(item.get("size") or item.get("amount")),
            "avg_price": _safe_float(avg_raw),
            "current_value": _safe_float(val_raw),
            "profit": _safe_float(pnl_raw),
        })
    return result


def normalize_pm_activity(
    items: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Normalize Polymarket /activity response."""
    result: list[dict[str, Any]] = []
    for item in items:
        price = _safe_float(item.get("price"))
        size_raw = item.get("size") if item.get("size") is not None else item.get("amount")
        size = _safe_float(size_raw)
        side_raw = str(item.get("side") or item.get("type") or "").upper()
        side = side_raw if side_raw in ("BUY", "SELL") else None
        ts_raw = item.get("timestamp") or item.get("ts")
        if isinstance(ts_raw, (int, float)) and ts_raw > 1e12:
            ts_raw = ts_raw / 1000  # ms → seconds
        ts_str: str | None = (
            datetime.fromtimestamp(ts_raw, tz=UTC).isoformat()
            if isinstance(ts_raw, (int, float))
            else (str(ts_raw) if ts_raw is not None else None)
        )
        result.append({
            "ts": ts_str,
            "market": item.get("market") or item.get("conditionId"),
            "outcome": item.get("outcome"),
            "price": round(price, 6) if price is not None else None,
            "size": round(size, 4) if size is not None else None,
            "side": side,
        })
    return result


def normalize_pm_value(
    items: list[dict[str, Any]],
) -> float | None:
    """Extract portfolio value from Polymarket /value response (first row)."""
    if not items:
        return None
    first = items[0]
    return _safe_float(
        first.get("value")
        if first.get("value") is not None
        else (first.get("portfolioValue") or first.get("totalValue"))
    )


def normalize_pm_whale_trades(
    items: list[dict[str, Any]],
    *,
    min_usd: float,
    limit: int,
    ref: str | None = None,
) -> list[dict[str, Any]]:
    """Filter and normalize trades where notional_usd (size * price) >= min_usd."""
    result: list[dict[str, Any]] = []
    for item in items:
        price = _safe_float(item.get("price"))
        size = _safe_float(item.get("size"))
        if price is None or size is None:
            continue
        notional_usd = round(price * size, 4)
        if notional_usd < min_usd:
            continue

        side_raw = str(item.get("side") or "").upper()
        side: str | None = side_raw if side_raw in ("BUY", "SELL") else None

        ts_raw = item.get("timestamp")
        if isinstance(ts_raw, (int, float)) and ts_raw > 1e12:
            ts_raw = ts_raw / 1000  # ms → seconds
        ts_str: str | None = (
            datetime.fromtimestamp(ts_raw, tz=UTC).isoformat()
            if isinstance(ts_raw, (int, float))
            else (str(ts_raw) if ts_raw is not None else None)
        )

        result.append({
            "ts": ts_str,
            "market": item.get("market") or item.get("conditionId") or item.get("asset_id") or item.get("asset"),
            "price": round(price, 6),
            "size": round(size, 4),
            "notional_usd": notional_usd,
            "side": side,
            "wallet": item.get("proxyWallet") or item.get("maker") or item.get("taker") or item.get("trader"),
            "pseudonym": item.get("pseudonym") or item.get("name"),
        })
        if len(result) >= limit:
            break
    return result
