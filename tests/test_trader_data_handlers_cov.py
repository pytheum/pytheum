"""Coverage tests for the trader-data handler edge paths.

Fills the branches the broad test_trader_data.py / test_trader_analytics.py
suites don't hit:
  • missing-clients (clients.kalshi / .polymarket is None) degraded 200s on
    book / trades / oi  (and the whale-trades None-client path)
  • PM resolve-error and PM fetch-error degradation on trades/oi
  • markets_trades param/freshness helpers: _parse_limit fallback,
    _parse_ts_epoch (string-float, ms divide, empty, garbage), _trade_freshness
    (empty -> (None, False); stale -> is_stale True)
  • whale-trades min_usd / limit parse fallbacks + the PM-resolve error path

All clients are in-memory stubs; no network, no sockets.
"""
from __future__ import annotations

import time
from typing import Any

from pytheum.api.markets_book import handle_market_book
from pytheum.api.markets_oi import handle_market_oi
from pytheum.api.markets_trades import (
    _parse_limit,
    _parse_ts_epoch,
    _trade_freshness,
    handle_market_trades,
)
from pytheum.api.markets_whale_trades import (
    _parse_limit as _whale_limit,
)
from pytheum.api.markets_whale_trades import (
    _parse_min_usd,
    handle_market_whale_trades,
)
from pytheum.trader.cache import SingleFlightCache

# ─────────────────────────────────────────────────────────────────────────────
# Stubs
# ─────────────────────────────────────────────────────────────────────────────


def _kalshi_none_clients() -> Any:
    class _C:
        kalshi = None      # venue==kalshi but client missing -> clients_not_ready
        polymarket = None
    return _C()


def _pm_none_clients() -> Any:
    class _C:
        kalshi = None
        polymarket = None
    return _C()


class _ErrGamma:
    async def get_market_by_slug(self, slug: str) -> tuple[Any, ...]:
        raise RuntimeError("gamma down")


def _pm_resolve_error_clients() -> Any:
    class _Data:
        async def get_trades(self, **kw: Any) -> tuple[Any, ...]:
            return [], {}

        async def get_open_interest(self, markets: list[str]) -> tuple[Any, ...]:
            return [], {}

    class _C:
        kalshi = None
        polymarket = type("_P", (), {"gamma": _ErrGamma(), "data": _Data()})()
    return _C()


# ─────────────────────────────────────────────────────────────────────────────
# missing-clients degraded 200s
# ─────────────────────────────────────────────────────────────────────────────


async def test_book_kalshi_client_none_degrades() -> None:
    status, body = await handle_market_book(
        "kalshi:KX", {}, clients=_kalshi_none_clients(), _cache=SingleFlightCache()
    )
    assert status == 200
    assert body["error"] == "clients_not_ready"
    assert body["source"] == "unavailable"


async def test_book_pm_client_none_degrades() -> None:
    status, body = await handle_market_book(
        "polymarket:slug", {}, clients=_pm_none_clients(), _cache=SingleFlightCache()
    )
    assert status == 200
    assert body["error"] == "clients_not_ready"


async def test_trades_kalshi_client_none_degrades() -> None:
    status, body = await handle_market_trades(
        "kalshi:KX", {}, clients=_kalshi_none_clients(), _cache=SingleFlightCache()
    )
    assert status == 200
    assert body["error"] == "clients_not_ready"


async def test_trades_pm_client_none_degrades() -> None:
    status, body = await handle_market_trades(
        "polymarket:slug", {}, clients=_pm_none_clients(), _cache=SingleFlightCache()
    )
    assert status == 200
    assert body["error"] == "clients_not_ready"


async def test_oi_kalshi_client_none_degrades() -> None:
    status, body = await handle_market_oi(
        "kalshi:KX", {}, clients=_kalshi_none_clients(), _cache=SingleFlightCache()
    )
    assert status == 200
    assert body["error"] == "clients_not_ready"


async def test_oi_pm_client_none_degrades() -> None:
    status, body = await handle_market_oi(
        "polymarket:slug", {}, clients=_pm_none_clients(), _cache=SingleFlightCache()
    )
    assert status == 200
    assert body["error"] == "clients_not_ready"


async def test_oi_unknown_venue() -> None:
    status, body = await handle_market_oi(
        "bareref", {}, clients=object(), _cache=SingleFlightCache()
    )
    assert status == 200
    assert body["error"] == "unknown_venue"


# ─────────────────────────────────────────────────────────────────────────────
# PM resolve-error degradation
# ─────────────────────────────────────────────────────────────────────────────


async def test_trades_pm_resolve_error_degrades() -> None:
    status, body = await handle_market_trades(
        "polymarket:slug", {}, clients=_pm_resolve_error_clients(),
        _cache=SingleFlightCache(),
    )
    assert status == 200
    assert body["error"] == "venue_unavailable"
    assert body["source"] == "unavailable"


async def test_oi_pm_resolve_error_degrades() -> None:
    status, body = await handle_market_oi(
        "polymarket:slug", {}, clients=_pm_resolve_error_clients(),
        _cache=SingleFlightCache(),
    )
    assert status == 200
    assert body["error"] == "venue_unavailable"


async def test_book_unknown_venue() -> None:
    status, body = await handle_market_book(
        "bareref", {}, clients=object(), _cache=SingleFlightCache()
    )
    assert status == 200
    assert body["error"] == "unknown_venue"


# ─────────────────────────────────────────────────────────────────────────────
# markets_trades param / freshness helpers
# ─────────────────────────────────────────────────────────────────────────────


def test_parse_limit_default_and_clamp_and_fallback() -> None:
    assert _parse_limit({}) == 100
    assert _parse_limit({"limit": "5000"}) == 1000   # clamped to max
    assert _parse_limit({"limit": "0"}) == 1          # clamped to min
    assert _parse_limit({"limit": "abc"}) == 100      # fallback


def test_parse_ts_epoch_variants() -> None:
    assert _parse_ts_epoch(None) is None
    assert _parse_ts_epoch("") is None
    assert _parse_ts_epoch("garbage") is None
    # int seconds
    assert _parse_ts_epoch(1700000000) == 1700000000.0
    # int ms -> divided to seconds
    assert _parse_ts_epoch(1_700_000_000_000) == 1700000000.0
    # ISO string
    iso = _parse_ts_epoch("2026-06-01T00:00:00Z")
    assert iso is not None and iso > 1e9
    # ISO naive (no tz) -> treated as UTC
    assert _parse_ts_epoch("2026-06-01T00:00:00") is not None
    # numeric STRING in ms -> divided
    assert _parse_ts_epoch("1700000000000") == 1700000000.0
    # numeric STRING in seconds -> as-is
    assert _parse_ts_epoch("1700000000") == 1700000000.0


def test_trade_freshness_empty_returns_none_false() -> None:
    assert _trade_freshness([]) == (None, False)
    # trades with no parseable ts also -> (None, False)
    assert _trade_freshness([{"ts": None}, {"ts": "garbage"}]) == (None, False)


def test_trade_freshness_recent_is_not_stale() -> None:
    now = time.time()
    age, stale = _trade_freshness([{"ts": now}])
    assert stale is False
    assert age is not None and age >= 0.0


def test_trade_freshness_old_is_stale() -> None:
    old = time.time() - (7 * 3600)  # 7h ago > 6h grace
    age, stale = _trade_freshness([{"ts": old}])
    assert stale is True
    assert age is not None and age > 21600.0


# ─────────────────────────────────────────────────────────────────────────────
# whale-trades param parse fallbacks + None-client
# ─────────────────────────────────────────────────────────────────────────────


def test_whale_parse_min_usd_fallbacks() -> None:
    assert _parse_min_usd({}) == 500.0
    assert _parse_min_usd({"min_usd": "abc"}) == 500.0
    assert _parse_min_usd({"min_usd": "-50"}) == 0.0   # clamped to >= 0
    assert _parse_min_usd({"min_usd": "123.5"}) == 123.5


def test_whale_parse_limit_fallbacks() -> None:
    assert _whale_limit({}) == 50
    assert _whale_limit({"limit": "abc"}) == 50
    assert _whale_limit({"limit": "999"}) == 500   # clamped to max
    assert _whale_limit({"limit": "0"}) == 1        # clamped to min


async def test_whale_no_client_degrades() -> None:
    status, body = await handle_market_whale_trades(
        {"min_usd": "100"}, clients=_pm_none_clients(), _cache=SingleFlightCache()
    )
    assert status == 200
    assert body["error"] == "clients_not_ready"


def test_book_parse_depth_fallback() -> None:
    from pytheum.api.markets_book import _parse_depth

    assert _parse_depth({}) == 20
    assert _parse_depth({"depth": "abc"}) == 20    # fallback
    assert _parse_depth({"depth": "999"}) == 200   # clamped to max
    assert _parse_depth({"depth": "0"}) == 1       # clamped to min


def _pm_fetch_error_clients(method: str) -> Any:
    """PM client that resolves fine but raises on the data/clob fetch call."""
    class _Gamma:
        async def get_market_by_slug(self, slug: str) -> tuple[Any, ...]:
            return {"clobTokenIds": ["tok"], "conditionId": "0xcond"}, {}

    class _Clob:
        async def get_book(self, token_id: str) -> tuple[Any, ...]:
            raise RuntimeError("clob down")

    class _Data:
        async def get_trades(self, **kw: Any) -> tuple[Any, ...]:
            raise RuntimeError("data trades down")

        async def get_open_interest(self, markets: list[str]) -> tuple[Any, ...]:
            raise RuntimeError("data oi down")

    class _C:
        kalshi = None
        polymarket = type("_P", (), {
            "gamma": _Gamma(), "clob": _Clob(), "data": _Data(),
        })()
    return _C()


async def test_book_pm_fetch_error_degrades() -> None:
    status, body = await handle_market_book(
        "polymarket:slug", {}, clients=_pm_fetch_error_clients("book"),
        _cache=SingleFlightCache(),
    )
    assert status == 200
    assert body["error"] == "venue_unavailable"
    assert "clob down" in body["detail"]


async def test_trades_pm_fetch_error_degrades() -> None:
    status, body = await handle_market_trades(
        "polymarket:slug", {}, clients=_pm_fetch_error_clients("trades"),
        _cache=SingleFlightCache(),
    )
    assert status == 200
    assert body["error"] == "venue_unavailable"


async def test_oi_pm_fetch_error_degrades() -> None:
    status, body = await handle_market_oi(
        "polymarket:slug", {}, clients=_pm_fetch_error_clients("oi"),
        _cache=SingleFlightCache(),
    )
    assert status == 200
    assert body["error"] == "venue_unavailable"


async def test_whale_pm_resolve_error_degrades() -> None:
    class _C:
        kalshi = None
        polymarket = type("_P", (), {
            "gamma": _ErrGamma(),
            "data": type("_D", (), {})(),
        })()

    status, body = await handle_market_whale_trades(
        {"market_ref": "polymarket:slug"}, clients=_C(), _cache=SingleFlightCache()
    )
    assert status == 200
    assert body["error"] == "venue_unavailable"
    assert body.get("ref") == "polymarket:slug"
