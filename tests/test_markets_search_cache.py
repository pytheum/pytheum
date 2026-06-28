"""Response-cache behavior for /v1/markets/search (#search serving-layer follow-up).

The search pipeline (DAO + annotators + serialize) used to re-run on every request, so the slow
high-cardinality terms agents hammer never recovered on repeat. These assert the SingleFlightCache
now (1) coalesces concurrent identical searches to one DAO pass, (2) serves repeats from cache,
(3) keys per query so distinct searches don't collide.
"""
from __future__ import annotations

import asyncio
from typing import Any

from pytheum.api.markets_search import handle_markets_search
from pytheum.trader.cache import SingleFlightCache


class _Dao:
    def __init__(self) -> None:
        self.calls = 0

    async def search_markets_by_title(self, tokens: list[str], *, venues: Any = None,
                                      statuses: Any = None, limit: int = 10) -> list[dict[str, Any]]:
        self.calls += 1
        await asyncio.sleep(0.02)  # let concurrent callers overlap inside the single-flight
        return [{"market_id": "kalshi:KXBTCD-1", "question": "Bitcoin price?", "venue": "kalshi",
                 "status": "active", "volume_usd": 1000.0, "payload": None}]


async def test_search_coalesces_caches_and_keys_per_query() -> None:
    dao = _Dao()
    cache = SingleFlightCache()
    q = {"q": "bitcoin", "limit": "50"}

    # 8 concurrent identical searches -> exactly ONE DAO pass (single-flight, no stampede).
    results = await asyncio.gather(*[
        handle_markets_search(dict(q), dao=dao, _cache=cache) for _ in range(8)])
    assert dao.calls == 1
    assert all(s == 200 and b["count"] == 1 for s, b in results)

    # repeat within TTL -> served from cache, no new DAO pass.
    await handle_markets_search(dict(q), dao=dao, _cache=cache)
    assert dao.calls == 1

    # a different query -> a new DAO pass (keys don't collide).
    _s, b2 = await handle_markets_search({"q": "ethereum"}, dao=dao, _cache=cache)
    assert dao.calls == 2 and b2["count"] == 1
