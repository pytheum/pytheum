"""Venue-aware whale-trades handler (api.markets_whale_trades).

Polymarket = wallet-level; Kalshi = anonymous size-based (no wallet). Kalshi mode reuses the
optional-ticker core client (ticker=None → global tape) + normalize_kalshi_whale_trades. The
single-flight test is the load-bearing one: K concurrent identical requests must collapse to ONE
upstream venue call (cache-stampede protection).
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

from pytheum.api.markets_whale_trades import handle_market_whale_trades
from pytheum.trader.cache import SingleFlightCache
from pytheum.trader.resolve import kalshi_ticker_from_ref


def _ktrade(*, side: str = "yes", price: float = 0.5, count: float = 2000.0,
            ticker: str = "KXFED-26-1", ts: str = "2026-06-28T00:00:00Z") -> dict[str, Any]:
    return {"taker_side": side, "yes_price_dollars": price, "no_price_dollars": price,
            "count_fp": count, "ticker": ticker, "created_time": ts}


class _KalshiRest:
    """Stub for kalshi.rest with a call counter + optional delay (to overlap concurrent calls)."""
    def __init__(self, body: dict[str, Any], *, delay: float = 0.0) -> None:
        self.body = body
        self.calls = 0
        self.delay = delay
        self.last_ticker: Any = "UNSET"

    async def get_trades_page(self, ticker: str | None = None, *, limit: int = 1000, **_kw: Any):
        self.calls += 1
        self.last_ticker = ticker
        if self.delay:
            await asyncio.sleep(self.delay)
        return self.body, None, None


def _clients(*, kalshi: Any = None, polymarket: Any = None) -> SimpleNamespace:
    return SimpleNamespace(
        kalshi=SimpleNamespace(rest=kalshi) if kalshi is not None else None,
        polymarket=polymarket,
    )


# whale: 2000 * 0.50 = $1000; minnow: 10 * 0.50 = $5
_BODY = {"trades": [_ktrade(count=2000, ticker="KXA-1"),
                    _ktrade(count=10, ticker="KXB-2"),
                    _ktrade(count=6000, ticker="KXC-3")]}  # $3000 whale


async def test_kalshi_global_whales_filtered_sorted_anonymous() -> None:
    rest = _KalshiRest(_BODY)
    status, body = await handle_market_whale_trades(
        {"venue": "kalshi", "min_usd": "500", "limit": "10"},
        clients=_clients(kalshi=rest), _cache=SingleFlightCache())
    assert status == 200
    assert body["venue"] == "kalshi" and body["source"] == "live"
    assert rest.last_ticker is None  # GLOBAL tape (no market_ref)
    trades = body["trades"]
    assert [t["notional_usd"] for t in trades] == [3000.0, 1000.0]  # filtered (>=500) + sorted desc
    assert all("wallet" not in t for t in trades)  # anonymity
    assert "anonymous" in body["note"].lower()


async def test_kalshi_per_market_passes_ticker() -> None:
    rest = _KalshiRest(_BODY)
    _status, body = await handle_market_whale_trades(
        {"market_ref": "kalshi:KXA-1", "min_usd": "500"},
        clients=_clients(kalshi=rest), _cache=SingleFlightCache())
    assert body["venue"] == "kalshi" and body.get("ref") == "kalshi:KXA-1"
    assert rest.last_ticker == kalshi_ticker_from_ref("kalshi:KXA-1")  # per-market, not None


async def test_min_usd_threshold_excludes_smaller_whales() -> None:
    rest = _KalshiRest(_BODY)
    _status, body = await handle_market_whale_trades(
        {"venue": "kalshi", "min_usd": "1500"},  # only the $3000 whale clears
        clients=_clients(kalshi=rest), _cache=SingleFlightCache())
    assert [t["notional_usd"] for t in body["trades"]] == [3000.0]


async def test_venue_mismatch_rejected() -> None:
    _status, body = await handle_market_whale_trades(
        {"venue": "kalshi", "market_ref": "polymarket:abc"},
        clients=_clients(kalshi=_KalshiRest(_BODY)), _cache=SingleFlightCache())
    assert body["error"] == "venue_mismatch"


async def test_bare_ref_rejected() -> None:
    _status, body = await handle_market_whale_trades(
        {"market_ref": "KXFED-26-1"},  # no venue prefix
        clients=_clients(kalshi=_KalshiRest(_BODY)), _cache=SingleFlightCache())
    assert body["error"] == "invalid_market_ref"


async def test_kalshi_clients_not_ready() -> None:
    _status, body = await handle_market_whale_trades(
        {"venue": "kalshi"}, clients=_clients(kalshi=None), _cache=SingleFlightCache())
    assert body["error"] == "clients_not_ready" and body["venue"] == "kalshi"


async def test_kalshi_venue_error_returns_200_unavailable() -> None:
    class _Boom(_KalshiRest):
        async def get_trades_page(self, ticker=None, *, limit=1000, **_kw):  # noqa: ANN001
            raise RuntimeError("kalshi 503")
    status, body = await handle_market_whale_trades(
        {"venue": "kalshi"}, clients=_clients(kalshi=_Boom({})), _cache=SingleFlightCache())
    assert status == 200 and body["source"] == "unavailable" and body["venue"] == "kalshi"


async def test_default_venue_is_polymarket_backcompat() -> None:
    # No venue + no market_ref → routes to the (unchanged) Polymarket path.
    async def _get_trades(*, markets=None, limit=0):  # noqa: ANN001
        return [], None
    pm = SimpleNamespace(data=SimpleNamespace(get_trades=_get_trades), gamma=None)
    _status, body = await handle_market_whale_trades(
        {}, clients=_clients(polymarket=pm), _cache=SingleFlightCache())
    assert body["venue"] == "polymarket" and body["count"] == 0


async def test_single_flight_collapses_concurrent_identical_requests() -> None:
    """K concurrent identical Kalshi whale requests → exactly ONE upstream get_trades_page call."""
    rest = _KalshiRest(_BODY, delay=0.05)  # delay so the K calls overlap inside the single-flight
    shared = SingleFlightCache()
    q = {"venue": "kalshi", "min_usd": "500", "limit": "10"}
    results = await asyncio.gather(*[
        handle_market_whale_trades(dict(q), clients=_clients(kalshi=rest), _cache=shared)
        for _ in range(8)
    ])
    assert rest.calls == 1  # coalesced — no stampede
    assert all(b["count"] == 2 for _s, b in results)  # every caller got the full result
