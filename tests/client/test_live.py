"""Real-integration tests — every SDK method against LIVE prod.

Gated: only runs with ``PYTHEUM_SDK_LIVE=1`` (keyless, so it needs no secret, but it
does hit the network + the per-IP rate limit, so it's off by default).

    PYTHEUM_SDK_LIVE=1 .venv/bin/python -m pytest tests/client/test_live.py -q
"""
from __future__ import annotations

import os

import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("PYTHEUM_SDK_LIVE") != "1",
    reason="live integration — set PYTHEUM_SDK_LIVE=1 to run",
)


@pytest.fixture()
def client():
    from pytheum.client import Client
    c = Client(max_concurrency=4)
    yield c
    c.close()


def _a_ref(client) -> str:
    """A real market ref that exists right now (from the matched set)."""
    page = client.matched_pairs(limit=1)
    return page["pairs"][0]["kalshi"]["id"]


def test_meta(client):
    st = client.status()
    assert st["equivalence"]["pairs_loaded"] > 100_000
    assert isinstance(client.about(), dict)
    assert isinstance(client.guide(), dict)
    assert isinstance(client.quality(), dict)


def test_discovery(client):
    sr = client.search("bitcoin", limit=5)
    assert sr["markets"] and all("id" in m for m in sr["markets"])
    sc = client.screen(limit=5)
    assert "markets" in sc or "results" in sc
    core = client.get_market(_a_ref(client))
    assert isinstance(core, dict)


def test_cross_venue(client):
    mp = client.matched_pairs(sort_by="net_edge", limit=10)
    assert mp["total"] > 100_000 and len(mp["pairs"]) <= 10
    ref = mp["pairs"][0]["kalshi"]["id"]
    eq = client.equivalents(ref, limit=5)
    assert isinstance(eq, (dict, list))
    assert isinstance(client.related(ref, limit=5), (dict, list))
    assert isinstance(client.rules(ref), (dict, list))
    div = client.find_divergences(limit=5)  # convenience over matched sort
    assert "pairs" in div


def test_trader_intel(client):
    lb = client.leaderboard(period="weekly")
    assert isinstance(lb, (dict, list))
    wt = client.whale_trades(limit=5)
    assert isinstance(wt, (dict, list))


def test_market_data_tolerant(client):
    """Book/trades/oi may be empty at off-peak hours — assert they don't raise, shape-check loosely."""
    ref = _a_ref(client)
    for call in (client.orderbook, client.trades, client.open_interest):
        out = call(ref)
        assert out is None or isinstance(out, (dict, list))


@pytest.mark.asyncio
async def test_async_batch_fanout():
    from pytheum.client import AsyncClient
    async with AsyncClient(max_concurrency=4) as px:
        pairs = (await px.matched_pairs(limit=6))["pairs"]
        refs = [p["kalshi"]["id"] for p in pairs]
        cores = await px.get_markets(refs)
        assert len(cores) == len(refs)
        # gather across different endpoint classes
        st, sr = await px.gather(px.status(), px.search("election", limit=3))
        assert "equivalence" in st and "markets" in sr
