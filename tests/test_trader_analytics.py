"""Tests for trader analytics surface (P1).

Coverage:
  normalizers:
    normalize_pm_leaderboard  — structure, float parsing
    normalize_pm_holders      — structure
    normalize_pm_positions    — structure
    normalize_pm_activity     — timestamp conversion, side mapping
    normalize_pm_value        — float extraction, empty input
    normalize_pm_whale_trades — min_usd filter, limit, notional field

  handle_traders_leaderboard:
    success path, period param, error degradation, no-client degradation

  handle_trader_profile:
    success path, invalid wallet, error degradation

  handle_market_holders:
    success path (polymarket), Kalshi rejected, gamma error degradation

  handle_market_whale_trades:
    basic success, min_usd filter, coalescing (5 concurrent → 1 venue call),
    with market_ref, Kalshi market_ref rejected, error degradation

  MCP registrations:
    t_leaderboard, t_trader_profile, t_market_holders, t_whale_trades
"""
from __future__ import annotations

import asyncio
from typing import Any

import pytest

from pytheum.api.markets_holders import handle_market_holders
from pytheum.api.markets_whale_trades import handle_market_whale_trades
from pytheum.api.traders_leaderboard import handle_traders_leaderboard
from pytheum.api.traders_profile import handle_trader_profile
from pytheum.trader.cache import SingleFlightCache
from pytheum.trader.normalizers import (
    normalize_pm_activity,
    normalize_pm_holders,
    normalize_pm_leaderboard,
    normalize_pm_positions,
    normalize_pm_value,
    normalize_pm_whale_trades,
)

# ─────────────────────────────────────────────────────────────────────────────
# Fake data constants
# ─────────────────────────────────────────────────────────────────────────────

_FAKE_LEADERBOARD_ITEMS = [
    {"name": "Alice", "address": "0xabc", "profit": "1000.0", "volume": "50000.0", "rank": 1},
    {"name": "Bob",   "address": "0xdef", "profit": "800.0",  "volume": "40000.0", "rank": 2},
]

_FAKE_POSITIONS_ITEMS = [
    {
        "market": "0xcond1", "outcome": "YES", "size": "100.0",
        "avgPrice": "0.55", "currentValue": "65.0", "profit": "10.0",
    },
]

_FAKE_ACTIVITY_ITEMS = [
    {
        "market": "0xcond1", "outcome": "YES", "price": "0.55", "size": "100.0",
        "side": "BUY", "timestamp": 1_700_000_000_000,  # milliseconds
    },
    {
        "market": "0xcond2", "outcome": "NO", "price": "0.45", "size": "50.0",
        "side": "SELL", "timestamp": 1_700_000_060_000,
    },
]

_FAKE_VALUE_ITEMS = [{"value": "12345.67"}]

_FAKE_HOLDERS_ITEMS = [
    {"address": "0xaaa", "amount": "200.0", "outcome": "YES"},
    {"address": "0xbbb", "amount": "100.0", "outcome": "NO"},
]

# Raw trades: notional = price * size
#   row 0: 0.80 * 1000 = 800  (whale at min_usd=500)
#   row 1: 0.50 *  200 = 100  (below 500)
#   row 2: 0.90 *  600 = 540  (whale at min_usd=500)
_FAKE_TRADES_RAW = [
    {"price": "0.80", "size": "1000", "side": "BUY",  "timestamp": 1_700_000_000,
     "market": "0xcond1", "maker": "0xwhale1"},
    {"price": "0.50", "size": "200",  "side": "SELL", "timestamp": 1_700_000_060,
     "market": "0xcond1", "maker": "0xsmall"},
    {"price": "0.90", "size": "600",  "side": "BUY",  "timestamp": 1_700_000_120,
     "market": "0xcond2", "maker": "0xwhale2"},
]


# ─────────────────────────────────────────────────────────────────────────────
# Stub helpers
# ─────────────────────────────────────────────────────────────────────────────

class _FakeGamma:
    async def get_market_by_slug(self, slug: str) -> tuple:
        return {"clobTokenIds": ["token123"], "conditionId": "0xcond_holders"}, {}

    async def get_market_by_id(self, *, market_id: str) -> tuple:
        return {"clobTokenIds": ["token123"], "conditionId": "0xcond_holders"}, {}

    async def get_market_by_condition_id(self, condition_id: str) -> tuple:
        return {"clobTokenIds": ["token123"], "conditionId": condition_id}, {}


class _FakeData:
    def __init__(self, call_counter: dict[str, int] | None = None,
                 latency: float = 0.0) -> None:
        self._calls = call_counter if call_counter is not None else {}
        self._latency = latency

    async def get_leaderboard(self, *, period: str = "weekly") -> tuple:
        await asyncio.sleep(self._latency)
        self._calls["get_leaderboard"] = self._calls.get("get_leaderboard", 0) + 1
        return _FAKE_LEADERBOARD_ITEMS, {}

    async def get_positions(self, *, user: str | None = None, market: str | None = None) -> tuple:
        return _FAKE_POSITIONS_ITEMS, {}

    async def get_activity(self, *, user: str | None = None,
                           limit: int = 100, offset: int = 0) -> tuple:
        return _FAKE_ACTIVITY_ITEMS, {}

    async def get_value(self, *, user: str | None = None,
                        limit: int = 100, offset: int = 0) -> tuple:
        return _FAKE_VALUE_ITEMS, {}

    async def get_holders(self, *, market: str) -> tuple:
        return _FAKE_HOLDERS_ITEMS, {}

    async def get_trades(self, *, markets: list[str] | None = None,
                         event_ids: Any = None, limit: int = 100,
                         offset: int = 0, side: Any = None) -> tuple:
        await asyncio.sleep(self._latency)
        self._calls["get_trades"] = self._calls.get("get_trades", 0) + 1
        return _FAKE_TRADES_RAW, {}


def _make_pm_clients(data_calls: dict[str, int] | None = None,
                     latency: float = 0.0) -> Any:
    class _C:
        kalshi = None
        polymarket = type("_P", (), {
            "gamma": _FakeGamma(),
            "data": _FakeData(data_calls, latency),
        })()
    return _C()


def _make_no_clients() -> Any:
    class _C:
        kalshi = None
        polymarket = None
    return _C()


# ─────────────────────────────────────────────────────────────────────────────
# Normalizer unit tests
# ─────────────────────────────────────────────────────────────────────────────

def test_normalize_pm_leaderboard_structure() -> None:
    result = normalize_pm_leaderboard(_FAKE_LEADERBOARD_ITEMS, period="weekly")
    assert result["period"] == "weekly"
    assert result["venue"] == "polymarket"
    assert result["source"] == "live"
    assert len(result["traders"]) == 2
    t0 = result["traders"][0]
    assert t0["name"] == "Alice"
    assert isinstance(t0["profit"], float)
    assert t0["profit"] == 1000.0


def test_normalize_pm_leaderboard_monthly() -> None:
    result = normalize_pm_leaderboard(_FAKE_LEADERBOARD_ITEMS, period="monthly")
    assert result["period"] == "monthly"
    assert result["count"] == 2


def test_normalize_pm_holders_structure() -> None:
    result = normalize_pm_holders(_FAKE_HOLDERS_ITEMS, ref="polymarket:some-slug")
    assert result["venue"] == "polymarket"
    assert result["source"] == "live"
    assert result["ref"] == "polymarket:some-slug"
    assert len(result["holders"]) == 2
    assert result["holders"][0]["outcome"] == "YES"
    assert result["holders"][1]["outcome"] == "NO"
    assert isinstance(result["holders"][0]["amount"], float)


def test_normalize_pm_positions_structure() -> None:
    positions = normalize_pm_positions(_FAKE_POSITIONS_ITEMS)
    assert len(positions) == 1
    p = positions[0]
    assert p["market"] == "0xcond1"
    assert p["outcome"] == "YES"
    assert isinstance(p["avg_price"], float)
    assert p["avg_price"] == pytest.approx(0.55)
    assert isinstance(p["profit"], float)


def test_normalize_pm_activity_ts_conversion() -> None:
    activity = normalize_pm_activity(_FAKE_ACTIVITY_ITEMS)
    assert len(activity) == 2
    # millisecond timestamps → ISO strings
    assert "T" in activity[0]["ts"]
    assert activity[0]["side"] == "BUY"
    assert activity[1]["side"] == "SELL"
    assert isinstance(activity[0]["price"], float)


def test_normalize_pm_value_extracts_float() -> None:
    val = normalize_pm_value(_FAKE_VALUE_ITEMS)
    assert isinstance(val, float)
    assert val == pytest.approx(12345.67)


def test_normalize_pm_value_empty_returns_none() -> None:
    assert normalize_pm_value([]) is None


def test_normalize_pm_whale_trades_min_usd_filter() -> None:
    # 800 and 540 pass; 100 does not
    result = normalize_pm_whale_trades(_FAKE_TRADES_RAW, min_usd=500, limit=10, ref=None)
    assert len(result) == 2
    assert all(t["notional_usd"] >= 500 for t in result)


def test_normalize_pm_whale_trades_limit() -> None:
    result = normalize_pm_whale_trades(_FAKE_TRADES_RAW, min_usd=1, limit=1, ref=None)
    assert len(result) == 1


def test_normalize_pm_whale_trades_notional_field() -> None:
    result = normalize_pm_whale_trades(_FAKE_TRADES_RAW, min_usd=1, limit=10, ref=None)
    for t in result:
        assert "notional_usd" in t
        assert t["notional_usd"] == pytest.approx(t["price"] * t["size"], rel=1e-4)


def test_normalize_pm_whale_trades_wallet_field() -> None:
    result = normalize_pm_whale_trades(_FAKE_TRADES_RAW, min_usd=1, limit=10, ref=None)
    assert result[0]["wallet"] == "0xwhale1"


# ─────────────────────────────────────────────────────────────────────────────
# handle_traders_leaderboard
# ─────────────────────────────────────────────────────────────────────────────

async def test_leaderboard_success() -> None:
    clients = _make_pm_clients()
    cache = SingleFlightCache()
    status, body = await handle_traders_leaderboard({}, clients=clients, _cache=cache)
    assert status == 200
    assert body["venue"] == "polymarket"
    assert body["source"] == "live"
    assert len(body["traders"]) == 2


async def test_leaderboard_period_monthly() -> None:
    data_calls: dict[str, int] = {}
    clients = _make_pm_clients(data_calls=data_calls)
    cache = SingleFlightCache()
    status, body = await handle_traders_leaderboard({"period": "monthly"}, clients=clients,
                                                    _cache=cache)
    assert status == 200
    assert body["period"] == "monthly"
    assert data_calls.get("get_leaderboard", 0) == 1


async def test_leaderboard_invalid_period_falls_back_to_weekly() -> None:
    clients = _make_pm_clients()
    cache = SingleFlightCache()
    status, body = await handle_traders_leaderboard({"period": "annual"}, clients=clients,
                                                    _cache=cache)
    assert status == 200
    assert body["period"] == "weekly"  # fallback


async def test_leaderboard_no_client_degrades() -> None:
    cache = SingleFlightCache()
    status, body = await handle_traders_leaderboard({}, clients=_make_no_clients(), _cache=cache)
    assert status == 200
    assert body["error"] == "clients_not_ready"


async def test_leaderboard_venue_error_degrades() -> None:
    class _ErrorData:
        async def get_leaderboard(self, *, period: str = "weekly") -> tuple:
            raise RuntimeError("upstream down")

    class _C:
        kalshi = None
        polymarket = type("_P", (), {"data": _ErrorData()})()

    cache = SingleFlightCache()
    status, body = await handle_traders_leaderboard({}, clients=_C(), _cache=cache)
    assert status == 200
    assert body["error"] == "venue_unavailable"
    assert body["source"] == "unavailable"
    assert "upstream down" in body["detail"]


# ─────────────────────────────────────────────────────────────────────────────
# handle_trader_profile
# ─────────────────────────────────────────────────────────────────────────────

_VALID_WALLET = "0xabc123def456789abc123def456789abc12345678"


async def test_trader_profile_success() -> None:
    clients = _make_pm_clients()
    cache = SingleFlightCache()
    status, body = await handle_trader_profile(_VALID_WALLET, {}, clients=clients, _cache=cache)
    assert status == 200
    assert body["wallet"] == _VALID_WALLET
    assert isinstance(body["positions"], list)
    assert isinstance(body["activity"], list)
    assert body.get("meta", {}).get("venue") == "polymarket"
    assert body.get("meta", {}).get("source") == "live"


async def test_trader_profile_value_present() -> None:
    clients = _make_pm_clients()
    cache = SingleFlightCache()
    status, body = await handle_trader_profile(_VALID_WALLET, {}, clients=clients, _cache=cache)
    assert status == 200
    assert isinstance(body["value"], float)


async def test_trader_profile_invalid_wallet() -> None:
    clients = _make_pm_clients()
    cache = SingleFlightCache()
    status, body = await handle_trader_profile("!!!invalid!!!", {}, clients=clients, _cache=cache)
    assert status == 200
    assert body["error"] == "invalid_wallet"


async def test_trader_profile_username_accepted() -> None:
    """Alphanumeric usernames (3–64 chars) must pass validation."""
    clients = _make_pm_clients()
    cache = SingleFlightCache()
    status, body = await handle_trader_profile("alice_trader", {}, clients=clients, _cache=cache)
    # Should not return invalid_wallet; any venue error is acceptable
    assert status == 200
    assert body.get("error") != "invalid_wallet"


async def test_trader_profile_no_client_degrades() -> None:
    cache = SingleFlightCache()
    status, body = await handle_trader_profile(
        _VALID_WALLET, {}, clients=_make_no_clients(), _cache=cache
    )
    assert status == 200
    assert body["error"] == "clients_not_ready"


async def test_trader_profile_venue_error_degrades() -> None:
    class _ErrorData:
        async def get_positions(self, **kwargs: Any) -> tuple:
            raise RuntimeError("positions down")
        async def get_activity(self, **kwargs: Any) -> tuple:
            return [], {}
        async def get_value(self, **kwargs: Any) -> tuple:
            return [], {}

    class _C:
        kalshi = None
        polymarket = type("_P", (), {"data": _ErrorData()})()

    cache = SingleFlightCache()
    status, body = await handle_trader_profile(_VALID_WALLET, {}, clients=_C(), _cache=cache)
    assert status == 200
    assert body["error"] == "venue_unavailable"
    assert body["source"] == "unavailable"
    assert body["wallet"] == _VALID_WALLET


# ─────────────────────────────────────────────────────────────────────────────
# handle_market_holders
# ─────────────────────────────────────────────────────────────────────────────

async def test_holders_pm_path() -> None:
    clients = _make_pm_clients()
    cache = SingleFlightCache()
    status, body = await handle_market_holders(
        "polymarket:some-slug", {}, clients=clients, _cache=cache
    )
    assert status == 200
    assert body["venue"] == "polymarket"
    assert body["source"] == "live"
    assert isinstance(body["holders"], list)
    assert body["count"] == 2


async def test_holders_kalshi_rejected() -> None:
    clients = _make_pm_clients()
    cache = SingleFlightCache()
    status, body = await handle_market_holders(
        "kalshi:KXTEST-25-YES", {}, clients=clients, _cache=cache
    )
    assert status == 200
    assert body["error"] == "polymarket_only"


async def test_holders_unknown_venue_rejected() -> None:
    clients = _make_pm_clients()
    cache = SingleFlightCache()
    status, body = await handle_market_holders("bare-ref", {}, clients=clients, _cache=cache)
    assert status == 200
    assert body["error"] == "polymarket_only"


async def test_holders_no_client_degrades() -> None:
    cache = SingleFlightCache()
    status, body = await handle_market_holders(
        "polymarket:some-slug", {}, clients=_make_no_clients(), _cache=cache
    )
    assert status == 200
    assert body["error"] == "clients_not_ready"


async def test_holders_gamma_error_degrades() -> None:
    class _ErrorGamma:
        async def get_market_by_slug(self, slug: str) -> tuple:
            raise RuntimeError("gamma down")

    class _C:
        kalshi = None
        polymarket = type("_P", (), {
            "gamma": _ErrorGamma(),
            "data": _FakeData(),
        })()

    cache = SingleFlightCache()
    status, body = await handle_market_holders(
        "polymarket:some-slug", {}, clients=_C(), _cache=cache
    )
    assert status == 200
    assert body["error"] == "venue_unavailable"
    assert body["source"] == "unavailable"


async def test_holders_data_error_degrades() -> None:
    class _ErrorData:
        async def get_holders(self, *, market: str) -> tuple:
            raise RuntimeError("data api down")

    class _C:
        kalshi = None
        polymarket = type("_P", (), {
            "gamma": _FakeGamma(),
            "data": _ErrorData(),
        })()

    cache = SingleFlightCache()
    status, body = await handle_market_holders(
        "polymarket:some-slug", {}, clients=_C(), _cache=cache
    )
    assert status == 200
    assert body["error"] == "venue_unavailable"


# ─────────────────────────────────────────────────────────────────────────────
# handle_market_whale_trades
# ─────────────────────────────────────────────────────────────────────────────

async def test_whale_trades_basic() -> None:
    clients = _make_pm_clients()
    cache = SingleFlightCache()
    status, body = await handle_market_whale_trades(
        {"min_usd": "500", "limit": "10"}, clients=clients, _cache=cache
    )
    assert status == 200
    assert body["venue"] == "polymarket"
    assert body["source"] == "live"
    assert all(t["notional_usd"] >= 500 for t in body["trades"])
    assert body["min_usd"] == 500.0


async def test_whale_trades_min_usd_filter() -> None:
    clients = _make_pm_clients()
    cache = SingleFlightCache()
    # notionals: 800, 100, 540 — only 800 passes min_usd=600
    status, body = await handle_market_whale_trades(
        {"min_usd": "600", "limit": "10"}, clients=clients, _cache=cache
    )
    assert status == 200
    assert len(body["trades"]) == 1
    assert body["trades"][0]["notional_usd"] == pytest.approx(800.0, rel=1e-4)


async def test_whale_trades_coalescing() -> None:
    """5 concurrent requests for the same key must share ONE venue fetch."""
    data_calls: dict[str, int] = {}
    clients = _make_pm_clients(data_calls=data_calls, latency=0.05)
    cache = SingleFlightCache()

    tasks = [
        handle_market_whale_trades({"min_usd": "100", "limit": "5"},
                                   clients=clients, _cache=cache)
        for _ in range(5)
    ]
    results = await asyncio.gather(*tasks)
    assert all(r[0] == 200 for r in results)
    # Coalescing: exactly 1 underlying data.get_trades call
    assert data_calls.get("get_trades", 0) == 1, (
        f"Expected 1 coalesced call, got {data_calls.get('get_trades', 0)}"
    )


async def test_whale_trades_with_market_ref() -> None:
    clients = _make_pm_clients()
    cache = SingleFlightCache()
    status, body = await handle_market_whale_trades(
        {"min_usd": "1", "limit": "10", "market_ref": "polymarket:some-slug"},
        clients=clients,
        _cache=cache,
    )
    assert status == 200
    assert body.get("ref") == "polymarket:some-slug"


async def test_whale_trades_kalshi_market_ref_rejected() -> None:
    clients = _make_pm_clients()
    cache = SingleFlightCache()
    status, body = await handle_market_whale_trades(
        {"market_ref": "kalshi:KXTEST-25-YES"}, clients=clients, _cache=cache
    )
    assert status == 200
    assert body["error"] == "polymarket_only"


async def test_whale_trades_no_client_degrades() -> None:
    cache = SingleFlightCache()
    status, body = await handle_market_whale_trades(
        {"min_usd": "100"}, clients=_make_no_clients(), _cache=cache
    )
    assert status == 200
    assert body["error"] == "clients_not_ready"


async def test_whale_trades_venue_error_degrades() -> None:
    class _ErrorData:
        async def get_trades(self, **kwargs: Any) -> tuple:
            raise ConnectionError("network error")

    class _C:
        kalshi = None
        polymarket = type("_P", (), {
            "gamma": _FakeGamma(),
            "data": _ErrorData(),
        })()

    cache = SingleFlightCache()
    status, body = await handle_market_whale_trades(
        {"min_usd": "100"}, clients=_C(), _cache=cache
    )
    assert status == 200
    assert body["error"] == "venue_unavailable"
    assert body["source"] == "unavailable"


async def test_whale_trades_note_present() -> None:
    """Response must always carry the Polymarket-only disclosure note."""
    clients = _make_pm_clients()
    cache = SingleFlightCache()
    status, body = await handle_market_whale_trades({}, clients=clients, _cache=cache)
    assert status == 200
    assert "note" in body
    assert "Kalshi" in body["note"]


# ─────────────────────────────────────────────────────────────────────────────
# MCP tool registration
# ─────────────────────────────────────────────────────────────────────────────

def test_mcp_trader_analytics_tools_registered() -> None:
    """t_leaderboard, t_trader_profile, t_market_holders, t_whale_trades are
    registered on the shared MCP instance."""
    from pytheum.mcp.server import mcp

    tool_names = {t.name for t in mcp._tool_manager.list_tools()}
    assert "t_leaderboard" in tool_names, f"t_leaderboard missing from {tool_names}"
    assert "t_trader_profile" in tool_names, f"t_trader_profile missing from {tool_names}"
    assert "t_market_holders" in tool_names, f"t_market_holders missing from {tool_names}"
    assert "t_whale_trades" in tool_names, f"t_whale_trades missing from {tool_names}"


def test_mcp_existing_trader_tools_still_registered() -> None:
    """Existing tools (t_orderbook, t_recent_trades, t_open_interest) must survive
    the new additions."""
    from pytheum.mcp.server import mcp

    tool_names = {t.name for t in mcp._tool_manager.list_tools()}
    assert "t_orderbook" in tool_names
    assert "t_recent_trades" in tool_names
    assert "t_open_interest" in tool_names
