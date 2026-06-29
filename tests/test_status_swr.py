"""Test /v1/status stale-while-revalidate: the slow venue-count never blocks a request."""
import asyncio
import time

import pytest

import pytheum.api.status as S


class _Dao:
    def __init__(self):
        self.calls = 0

    async def fetch_venue_stats(self):
        self.calls += 1
        return [{"venue": "kalshi", "count": 192000, "last_updated": "2026-06-28T00:00:00Z"}]


class _Idx:
    pairs_loaded = 100
    dataset_version = "v1"


def _reset():
    S._cache = None
    S._refreshing = False


@pytest.mark.asyncio
async def test_cold_builds_once_then_serves_cache():
    _reset()
    dao = _Dao()
    st, body = await S.handle_status({}, dao=dao, equivalence=_Idx(), related=_Idx())
    assert st == 200 and dao.calls == 1
    assert body["platforms"]["kalshi"]["markets"] == 192000
    # fresh cache → served without another DAO round-trip
    st, body = await S.handle_status({}, dao=dao, equivalence=_Idx(), related=_Idx())
    assert dao.calls == 1


@pytest.mark.asyncio
async def test_stale_serves_immediately_and_refreshes_in_background():
    _reset()
    dao = _Dao()
    await S.handle_status({}, dao=dao, equivalence=_Idx(), related=_Idx())  # warms cache
    assert dao.calls == 1
    # force the cache stale
    S._cache = (time.monotonic() - (S._CACHE_TTL_S + 1), S._cache[1])
    t0 = time.monotonic()
    st, body = await S.handle_status({}, dao=dao, equivalence=_Idx(), related=_Idx())
    served_ms = (time.monotonic() - t0) * 1000
    assert st == 200 and served_ms < 50          # served stale immediately, did NOT block
    await asyncio.sleep(0.05)                      # let the background refresh run
    assert dao.calls == 2                          # refreshed out of band
