"""Coverage tests for pytheum.mcp.server.

Two areas:
  1. The @mcp.tool()/@enveloped wrapper bodies (t_status … t_whale_trades) —
     called directly with the HTTP layer (tools._get) patched so each wrapper's
     param-building + delegation line executes without a live server.
  2. The remote-connector plumbing: _client_ip (x-forwarded-for + scope.client +
     unknown), _allow (token-bucket allow/deny + the >50k eviction branches),
     and _build_http_app (CORS wrapper + 429 rate-limit gate) — driven through a
     pure ASGI harness, no port bind, no sockets.

NEVER opens a socket: tools._get is monkeypatched to an in-memory stub and the
ASGI app is invoked directly with fake send/receive callables.
"""
from __future__ import annotations

from typing import Any

import pytest

import pytheum.mcp.server as server
import pytheum.mcp.tools as tools

# ─────────────────────────────────────────────────────────────────────────────
# Tool-wrapper bodies — patch the HTTP layer + agent_guide
# ─────────────────────────────────────────────────────────────────────────────


@pytest.fixture
def captured(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Patch tools._get + tools._get_market so wrappers never hit the network.

    Records the last (path, params) so each wrapper's delegation is asserted.
    Also disables crypto-spot enrichment (network) for the screen/find wrappers.
    """
    cap: dict[str, Any] = {}

    async def _fake_get(path: str, params: dict[str, Any], base_url: str) -> dict[str, Any]:
        cap["path"] = path
        cap["params"] = dict(params)
        cap["base_url"] = base_url
        return {"stub": True, "path": path}

    async def _fake_get_market(path: str, params: dict[str, Any], base_url: str,
                               **kw: Any) -> dict[str, Any]:
        cap["path"] = path
        cap["params"] = dict(params)
        cap["kw"] = kw
        return {"stub": True, "path": path}

    monkeypatch.setattr(tools, "_get", _fake_get)
    monkeypatch.setattr(tools, "_get_market", _fake_get_market)

    async def _noop(rows: Any) -> None:
        return None

    # _enrich_crypto_spot exists on tools and is called by screen/find/search.
    if hasattr(tools, "_enrich_crypto_spot"):
        monkeypatch.setattr(tools, "_enrich_crypto_spot", _noop)
    return cap


def _data(envelope: dict[str, Any]) -> Any:
    """Pull the payload out of the {ok, command, data, meta} envelope."""
    assert envelope["ok"] is True, envelope
    return envelope["data"]


async def test_t_status(captured: dict[str, Any]) -> None:
    env = await server.t_status()
    assert env["command"] == "t_status"
    assert captured["path"] == "/v1/status"


async def test_t_orderbook(captured: dict[str, Any]) -> None:
    env = await server.t_orderbook("kalshi:KX", depth=7)
    assert env["command"] == "t_orderbook"
    assert captured["params"]["depth"] == 7
    assert "/book" in captured["path"]


async def test_t_recent_trades(captured: dict[str, Any]) -> None:
    env = await server.t_recent_trades("kalshi:KX", limit=12)
    assert env["command"] == "t_recent_trades"
    assert captured["params"]["limit"] == 12
    assert "/trades" in captured["path"]


async def test_t_open_interest(captured: dict[str, Any]) -> None:
    env = await server.t_open_interest("kalshi:KX")
    assert env["command"] == "t_open_interest"
    assert "/oi" in captured["path"]


async def test_t_ohlcv(captured: dict[str, Any]) -> None:
    env = await server.t_ohlcv("kalshi:KX", interval="5m", limit=33)
    assert env["command"] == "t_ohlcv"
    assert captured["params"]["interval"] == "5m"
    assert captured["params"]["limit"] == 33
    assert "/ohlcv" in captured["path"]


async def test_t_leaderboard(captured: dict[str, Any]) -> None:
    env = await server.t_leaderboard(period="monthly")
    assert env["command"] == "t_leaderboard"
    assert captured["params"]["period"] == "monthly"
    assert captured["path"] == "/v1/traders/leaderboard"


async def test_t_trader_profile(captured: dict[str, Any]) -> None:
    env = await server.t_trader_profile("0xabc")
    assert env["command"] == "t_trader_profile"
    assert "/v1/traders/" in captured["path"]


async def test_t_market_holders(captured: dict[str, Any]) -> None:
    env = await server.t_market_holders("polymarket:slug")
    assert env["command"] == "t_market_holders"
    assert "/holders" in captured["path"]


async def test_t_whale_trades_no_ref(captured: dict[str, Any]) -> None:
    env = await server.t_whale_trades(min_usd=250, limit=5)
    assert env["command"] == "t_whale_trades"
    assert captured["path"] == "/v1/markets/whale-trades"
    assert captured["params"]["min_usd"] == 250
    assert "market_ref" not in captured["params"]


async def test_t_whale_trades_with_ref(captured: dict[str, Any]) -> None:
    env = await server.t_whale_trades(market_ref="polymarket:slug")
    assert env["command"] == "t_whale_trades"
    assert captured["params"]["market_ref"] == "polymarket:slug"


async def test_t_get_market(captured: dict[str, Any]) -> None:
    env = await server.t_get_market("kalshi:KX")
    assert env["command"] == "t_get_market"


async def test_t_equivalent_markets(captured: dict[str, Any]) -> None:
    env = await server.t_equivalent_markets("kalshi:KX")
    assert env["command"] == "t_equivalent_markets"


async def test_t_related_markets(captured: dict[str, Any]) -> None:
    env = await server.t_related_markets("kalshi:KX")
    assert env["command"] == "t_related_markets"


async def test_t_market_rules(captured: dict[str, Any]) -> None:
    env = await server.t_market_rules("kalshi:KX")
    assert env["command"] == "t_market_rules"


async def test_t_market_context(captured: dict[str, Any]) -> None:
    env = await server.t_market_context("kalshi:KX", limit=3)
    assert env["command"] == "t_market_context"


async def test_t_bundle_context(captured: dict[str, Any]) -> None:
    env = await server.t_bundle_context("polymarket:soccer", limit=4)
    assert env["command"] == "t_bundle_context"


async def test_t_find_markets(captured: dict[str, Any]) -> None:
    env = await server.t_find_markets("super bowl", limit=5)
    assert env["command"] == "t_find_markets"


async def test_t_event_related_markets(captured: dict[str, Any]) -> None:
    env = await server.t_event_related_markets("evt_news_x")
    assert env["command"] == "t_event_related_markets"


async def test_t_market_history(captured: dict[str, Any]) -> None:
    env = await server.t_market_history("kalshi:KX", limit=10)
    assert env["command"] == "t_market_history"


async def test_t_market_flow(captured: dict[str, Any]) -> None:
    env = await server.t_market_flow("polymarket:slug", window_hours=12)
    assert env["command"] == "t_market_flow"


async def test_t_find_divergences(captured: dict[str, Any]) -> None:
    env = await server.t_find_divergences(min_net_edge=0.01, limit=3)
    assert env["command"] == "t_find_divergences"


async def test_t_matched_pairs(captured: dict[str, Any]) -> None:
    env = await server.t_matched_pairs(bet_type="moneyline", limit=4)
    assert env["command"] == "t_matched_pairs"


async def test_t_context_batch(captured: dict[str, Any]) -> None:
    env = await server.t_context_batch(["kalshi:KX"], limit=2)
    assert env["command"] == "t_context_batch"


async def test_t_screen(captured: dict[str, Any]) -> None:
    env = await server.t_screen(status="active", limit=5)
    assert env["command"] == "t_screen"


async def test_t_search_markets(captured: dict[str, Any]) -> None:
    env = await server.t_search_markets("btc", limit=5)
    assert env["command"] == "t_search_markets"


async def test_t_guide() -> None:
    env = await server.t_guide()
    assert env["command"] == "t_guide"
    assert env["ok"] is True
    # agent_guide is local (no network) and returns the playbook keys
    data = _data(env)
    assert "summary" in data


# ─────────────────────────────────────────────────────────────────────────────
# _client_ip
# ─────────────────────────────────────────────────────────────────────────────


def test_client_ip_x_forwarded_for_first_hop() -> None:
    scope = {"headers": [(b"x-forwarded-for", b"1.2.3.4, 5.6.7.8")]}
    assert server._client_ip(scope) == "1.2.3.4"


def test_client_ip_from_scope_client() -> None:
    scope = {"headers": [], "client": ("9.9.9.9", 12345)}
    assert server._client_ip(scope) == "9.9.9.9"


def test_client_ip_unknown_when_no_client() -> None:
    scope: dict[str, Any] = {"headers": [], "client": None}
    assert server._client_ip(scope) == "unknown"


# ─────────────────────────────────────────────────────────────────────────────
# _allow — token bucket + eviction
# ─────────────────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _clear_buckets() -> Any:
    server._buckets.clear()
    yield
    server._buckets.clear()


def test_allow_first_request_passes() -> None:
    assert server._allow("ip-a") is True


def test_allow_denies_when_tokens_exhausted(monkeypatch: pytest.MonkeyPatch) -> None:
    # Force a tiny bucket so the second call is denied.
    monkeypatch.setattr(server, "_RL_BURST", 1.0)
    monkeypatch.setattr(server, "_RL_PER_MIN", 0.0001)  # negligible refill
    assert server._allow("ip-b") is True   # consumes the single token
    assert server._allow("ip-b") is False  # < 1.0 token -> denied


def test_allow_eviction_prunes_stale(monkeypatch: pytest.MonkeyPatch) -> None:
    """When _buckets exceeds 50k, fully-refilled (stale) entries are pruned."""
    monkeypatch.setattr(server, "_RL_BURST", 60.0)
    monkeypatch.setattr(server, "_RL_PER_MIN", 60.0)
    # Seed >50k entries with a very old timestamp so they count as stale.
    old = 0.0
    server._buckets.clear()
    for i in range(50_001):
        server._buckets[f"stale-{i}"] = [60.0, old]
    # A fresh request triggers the prune path; stale entries are removed.
    assert server._allow("fresh-ip") is True
    assert len(server._buckets) < 50_001


def test_allow_eviction_safety_net_evicts_oldest(monkeypatch: pytest.MonkeyPatch) -> None:
    """If still >50k after stale-prune (none stale yet), the longest-idle
    entries are evicted by the safety net."""
    import time as _time

    monkeypatch.setattr(server, "_RL_BURST", 60.0)
    monkeypatch.setattr(server, "_RL_PER_MIN", 60.0)
    now = _time.monotonic()
    server._buckets.clear()
    # All entries are RECENT (not stale) so the first prune removes nothing and
    # the safety-net branch runs.
    for i in range(50_002):
        server._buckets[f"recent-{i}"] = [60.0, now + i * 1e-6]
    assert server._allow("another-ip") is True
    assert len(server._buckets) <= 50_001  # safety net trimmed back


# ─────────────────────────────────────────────────────────────────────────────
# _build_http_app — ASGI harness (no socket)
# ─────────────────────────────────────────────────────────────────────────────


async def _drive(app: Any, scope: dict[str, Any]) -> list[dict[str, Any]]:
    """Invoke an ASGI app once, capturing the messages it sends."""
    sent: list[dict[str, Any]] = []

    async def _receive() -> dict[str, Any]:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def _send(msg: dict[str, Any]) -> None:
        sent.append(msg)

    await app(scope, _receive, _send)
    return sent


async def test_build_http_app_rate_limited_returns_429(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(server, "_allow", lambda ip: False)
    app = server._build_http_app()
    scope = {"type": "http", "method": "GET", "path": "/mcp",
             "headers": [(b"x-forwarded-for", b"7.7.7.7")], "client": ("7.7.7.7", 1)}
    sent = await _drive(app, scope)
    start = next(m for m in sent if m["type"] == "http.response.start")
    assert start["status"] == 429
    body = next(m for m in sent if m["type"] == "http.response.body")
    assert b"rate_limited" in body["body"]
    # CORS header present on the 429 so browser clients don't see opaque failure
    header_keys = {k for k, _ in start["headers"]}
    assert b"access-control-allow-origin" in header_keys


async def test_build_http_app_cors_preflight_passes_through(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(server, "_allow", lambda ip: True)
    app = server._build_http_app()
    # An OPTIONS preflight with an Origin is handled by the CORS middleware and
    # returns 200 without reaching the MCP transport.
    scope = {
        "type": "http",
        "method": "OPTIONS",
        "path": "/mcp",
        "headers": [
            (b"origin", b"https://claude.ai"),
            (b"access-control-request-method", b"POST"),
            (b"access-control-request-headers", b"content-type"),
        ],
        "client": ("8.8.8.8", 1),
    }
    sent = await _drive(app, scope)
    start = next(m for m in sent if m["type"] == "http.response.start")
    assert start["status"] == 200
    header_keys = {k for k, _ in start["headers"]}
    assert b"access-control-allow-origin" in header_keys


async def test_build_http_app_lifespan_passes_through(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A non-http scope (lifespan) bypasses the rate-limit gate and reaches the
    inner app — covers the `await inner(...)` pass-through for non-http."""
    monkeypatch.setattr(server, "_allow", lambda ip: True)
    app = server._build_http_app()

    sent: list[dict[str, Any]] = []
    started = {"v": False}

    async def _receive() -> dict[str, Any]:
        if not started["v"]:
            started["v"] = True
            return {"type": "lifespan.startup"}
        return {"type": "lifespan.shutdown"}

    async def _send(msg: dict[str, Any]) -> None:
        sent.append(msg)

    # Starlette/MCP lifespan handler completes; we only assert it didn't 429.
    await app({"type": "lifespan"}, _receive, _send)
    assert all(m.get("status") != 429 for m in sent)


# ─────────────────────────────────────────────────────────────────────────────
# Per-tool usage emitter — _emit_usage / _ip_hash
# ─────────────────────────────────────────────────────────────────────────────


def test_emit_usage_writes_parseable_jsonl(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One call writes one JSONL line with ts/tool/ip_hash; the IP is hashed
    (not plaintext) and the salt feeds the digest."""
    import json

    log = tmp_path / "usage.jsonl"
    monkeypatch.setattr(server, "_USAGE_LOG", str(log))
    monkeypatch.setattr(server, "_USAGE_SALT", "test-salt")
    token = server._current_ip.set("203.0.113.9")
    try:
        server._emit_usage("t_status")
        server._emit_usage("t_screen")
    finally:
        server._current_ip.reset(token)

    lines = log.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    rec = json.loads(lines[0])
    assert set(rec) == {"ts", "tool", "ip_hash"}
    assert rec["tool"] == "t_status"
    assert isinstance(rec["ts"], float)
    # IP is hashed, never stored plaintext, and matches the salted digest.
    assert "203.0.113.9" not in lines[0]
    assert rec["ip_hash"] == server._ip_hash("203.0.113.9")
    assert len(rec["ip_hash"]) == 16


def test_emit_usage_never_raises_on_unwritable_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unwritable log path is silently a no-op — logging must never raise into
    the tool call."""
    monkeypatch.setattr(
        server, "_USAGE_LOG", "/this/path/does/not/exist/usage.jsonl")
    server._emit_usage("t_status")  # must not raise


def test_emit_usage_defaults_ip_to_unknown_when_no_scope(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With no request scope in flight the ip_hash is the hash of 'unknown'."""
    import json

    log = tmp_path / "usage.jsonl"
    monkeypatch.setattr(server, "_USAGE_LOG", str(log))
    # Fresh contextvar default is "unknown".
    server._emit_usage("t_guide")
    rec = json.loads(log.read_text(encoding="utf-8").splitlines()[-1])
    assert rec["ip_hash"] == server._ip_hash("unknown")
