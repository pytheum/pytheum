"""t_screen lean default (#264): the full bundle_outcomes ladder is ~28% of the
payload and bundle_top_outcome already carries the favorite, so drop the ladder
unless full=true. Verified live via the connector; this pins the behavior."""
from __future__ import annotations

import pytheum.mcp.tools as tools

_CANNED = {
    "markets": [
        {"id": "polymarket:30615", "venue": "polymarket",
         "bundle_top_outcome": {"outcome": "Spain", "implied_yes": 0.16},
         "bundle_outcomes": [{"outcome": "Spain"}, {"outcome": "France"}]},
        {"id": "kalshi:KXNBA", "venue": "kalshi", "implied_yes": 0.5},
    ],
    "meta": {"filters": {}},
}


async def _fake_get(path, params, base_url):
    return {"markets": [dict(m) for m in _CANNED["markets"]],
            "meta": {"filters": {}}}


def _patch(monkeypatch):
    monkeypatch.setattr(tools, "_get", _fake_get)

    async def _noop(rows):
        return None
    monkeypatch.setattr(tools, "_enrich_crypto_spot", _noop)


async def test_screen_lean_drops_ladder_keeps_favorite(monkeypatch):
    _patch(monkeypatch)
    resp = await tools.screen_markets(base_url="x", full=False)
    parent = next(m for m in resp["markets"] if m["id"] == "polymarket:30615")
    assert "bundle_outcomes" not in parent                 # ladder dropped
    assert parent["bundle_top_outcome"]["outcome"] == "Spain"  # favorite kept
    assert resp["meta"]["lean"] is True


async def test_screen_full_keeps_ladder(monkeypatch):
    _patch(monkeypatch)
    resp = await tools.screen_markets(base_url="x", full=True)
    parent = next(m for m in resp["markets"] if m["id"] == "polymarket:30615")
    assert parent["bundle_outcomes"] == [{"outcome": "Spain"}, {"outcome": "France"}]
    assert "lean" not in resp.get("meta", {})
