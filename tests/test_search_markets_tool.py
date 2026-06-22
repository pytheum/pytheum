"""MCP tool wrapper t_search_markets → search_markets (tools.search_markets).

Patches the HTTP layer (tools._get) so the wrapper's param-building, venue
normalization, validation, and empty-result self-explanation are tested without
a live server.
"""
from __future__ import annotations

from typing import Any

import pytheum.mcp.tools as tools


def _patch(monkeypatch, captured: dict[str, Any], canned: dict[str, Any]) -> None:
    async def _fake_get(path: str, params: dict[str, Any], base_url: str) -> dict[str, Any]:
        captured["path"] = path
        captured["params"] = params
        return {k: v for k, v in canned.items()}

    monkeypatch.setattr(tools, "_get", _fake_get)

    async def _noop(rows):
        return None
    monkeypatch.setattr(tools, "_enrich_crypto_spot", _noop)


async def test_search_markets_builds_params_and_hits_endpoint(monkeypatch) -> None:
    cap: dict[str, Any] = {}
    _patch(monkeypatch, cap, {"markets": [{"id": "kalshi:KXSB-26", "venue": "kalshi"}],
                              "count": 1, "meta": {}})
    resp = await tools.search_markets("super bowl", base_url="x", venue="kalshi", limit=10)
    assert cap["path"] == "/v1/markets/search"
    assert cap["params"]["q"] == "super bowl"
    assert cap["params"]["venues"] == "kalshi"
    assert cap["params"]["status"] == "active"
    assert cap["params"]["limit"] == 10
    assert resp["count"] == 1


async def test_search_markets_venue_alias_normalized(monkeypatch) -> None:
    cap: dict[str, Any] = {}
    _patch(monkeypatch, cap, {"markets": [{"id": "polymarket:1"}], "count": 1, "meta": {}})
    await tools.search_markets("x", base_url="x", venue="poly")
    assert cap["params"]["venues"] == "polymarket"  # alias folded


async def test_search_markets_all_venue_word_omits_filter(monkeypatch) -> None:
    cap: dict[str, Any] = {}
    _patch(monkeypatch, cap, {"markets": [], "count": 0, "meta": {}})
    await tools.search_markets("x", base_url="x", venue="all")
    assert "venues" not in cap["params"]  # 'all' → no venue filter


async def test_search_markets_empty_query_rejected_before_http(monkeypatch) -> None:
    cap: dict[str, Any] = {}
    _patch(monkeypatch, cap, {"markets": [], "count": 0})
    resp = await tools.search_markets("   ", base_url="x")
    assert resp["error"] == "invalid_query"
    assert "path" not in cap  # never hit the HTTP layer


async def test_search_markets_bad_venue_rejected(monkeypatch) -> None:
    cap: dict[str, Any] = {}
    _patch(monkeypatch, cap, {"markets": [], "count": 0})
    resp = await tools.search_markets("x", base_url="x", venue="binance")
    assert resp["error"] == "unknown_venue"
    assert "path" not in cap


async def test_search_markets_invalid_limit_rejected(monkeypatch) -> None:
    cap: dict[str, Any] = {}
    _patch(monkeypatch, cap, {"markets": [], "count": 0})
    resp = await tools.search_markets("x", base_url="x", limit=0)
    assert resp["error"] == "invalid_limit"
    assert "path" not in cap


async def test_search_markets_empty_result_gets_semantic_hint(monkeypatch) -> None:
    cap: dict[str, Any] = {}
    _patch(monkeypatch, cap, {"markets": [], "count": 0, "meta": {}})
    resp = await tools.search_markets("zzqqx", base_url="x")
    assert resp["count"] == 0
    assert "t_find_markets" in resp["meta"]["hint"]
