"""Tests for GET /v1/status."""
from __future__ import annotations

import pytest

from pytheum.api.status import _is_stale, handle_status

# ---------------------------------------------------------------------------
# _is_stale unit tests
# ---------------------------------------------------------------------------


def test_is_stale_none():
    assert _is_stale(None) is False


def test_is_stale_recent():
    from datetime import UTC, datetime, timedelta
    recent = (datetime.now(UTC) - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    assert _is_stale(recent) is False


def test_is_stale_old():
    from datetime import UTC, datetime, timedelta
    old = (datetime.now(UTC) - timedelta(hours=48)).strftime("%Y-%m-%dT%H:%M:%SZ")
    assert _is_stale(old) is True


def test_is_stale_exactly_threshold():
    """Just above threshold is stale."""
    from datetime import UTC, datetime, timedelta
    just_over = (datetime.now(UTC) - timedelta(seconds=24 * 3601)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    assert _is_stale(just_over) is True


def test_is_stale_bad_value():
    assert _is_stale("not-a-date") is False


# ---------------------------------------------------------------------------
# Fake deps
# ---------------------------------------------------------------------------


class _FakeEquivalence:
    pairs_loaded = 136877
    dataset_version = "2026-06-10T12:00:00Z"
    file_missing = False
    load_error = None


class _FakeRelated:
    pairs_loaded = 1097
    dataset_version = "2026-06-09T00:00:00Z"
    file_missing = False


class _FakeDao:
    """DAO with fetch_venue_stats returning two venues."""

    def __init__(self, *, rows=None):
        from datetime import UTC, datetime, timedelta
        fresh = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
        stale = (datetime.now(UTC) - timedelta(hours=30)).isoformat()
        self._rows = rows if rows is not None else [
            {"venue": "kalshi", "count": 45000, "last_updated": fresh},
            {"venue": "polymarket", "count": 90000, "last_updated": stale},
        ]

    async def fetch_venue_stats(self):
        return self._rows


class _FakeDaoNoStats:
    """DAO without fetch_venue_stats — platforms block should be absent."""
    pass


# ---------------------------------------------------------------------------
# Handler tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_handler_returns_200():
    from pytheum.api import status as _status_mod
    _status_mod._cache = None  # clear cache between tests
    dao = _FakeDao()
    code, body = await handle_status({}, dao=dao, equivalence=_FakeEquivalence(), related=_FakeRelated())
    assert code == 200


@pytest.mark.asyncio
async def test_handler_service_block():
    from pytheum.api import status as _status_mod
    _status_mod._cache = None
    dao = _FakeDaoNoStats()
    _, body = await handle_status({}, dao=dao)
    assert "service" in body
    assert "version" in body["service"]
    assert "now" in body["service"]


@pytest.mark.asyncio
async def test_handler_equivalence_block():
    from pytheum.api import status as _status_mod
    _status_mod._cache = None
    dao = _FakeDaoNoStats()
    _, body = await handle_status({}, dao=dao, equivalence=_FakeEquivalence())
    eq = body["equivalence"]
    assert eq["pairs_loaded"] == 136877
    assert eq["dataset_version"] == "2026-06-10T12:00:00Z"


@pytest.mark.asyncio
async def test_handler_related_block():
    from pytheum.api import status as _status_mod
    _status_mod._cache = None
    dao = _FakeDaoNoStats()
    _, body = await handle_status({}, dao=dao, related=_FakeRelated())
    assert body["related"]["pairs_loaded"] == 1097


@pytest.mark.asyncio
async def test_handler_platforms_block_present():
    from pytheum.api import status as _status_mod
    _status_mod._cache = None
    dao = _FakeDao()
    _, body = await handle_status({}, dao=dao, equivalence=_FakeEquivalence(), related=_FakeRelated())
    assert "platforms" in body
    assert "kalshi" in body["platforms"]
    assert "polymarket" in body["platforms"]


@pytest.mark.asyncio
async def test_handler_platforms_block_absent_without_dao_method():
    from pytheum.api import status as _status_mod
    _status_mod._cache = None
    dao = _FakeDaoNoStats()
    _, body = await handle_status({}, dao=dao)
    assert "platforms" not in body


@pytest.mark.asyncio
async def test_handler_stale_detection():
    """polymarket row has old last_updated → status='stale'."""
    from pytheum.api import status as _status_mod
    _status_mod._cache = None
    dao = _FakeDao()
    _, body = await handle_status({}, dao=dao, equivalence=_FakeEquivalence(), related=_FakeRelated())
    assert body["platforms"]["kalshi"]["status"] == "ok"
    assert body["platforms"]["polymarket"]["status"] == "stale"


@pytest.mark.asyncio
async def test_handler_platforms_has_markets_count():
    from pytheum.api import status as _status_mod
    _status_mod._cache = None
    dao = _FakeDao()
    _, body = await handle_status({}, dao=dao, equivalence=_FakeEquivalence(), related=_FakeRelated())
    assert body["platforms"]["kalshi"]["markets"] == 45000
    assert body["platforms"]["polymarket"]["markets"] == 90000


@pytest.mark.asyncio
async def test_handler_dao_failure_degrades_gracefully():
    """fetch_venue_stats raising must not 500 — platforms is just absent."""
    from pytheum.api import status as _status_mod
    _status_mod._cache = None

    class _FailingDao:
        async def fetch_venue_stats(self):
            raise RuntimeError("db is down")

    _, body = await handle_status({}, dao=_FailingDao())
    assert "service" in body
    assert "platforms" not in body


@pytest.mark.asyncio
async def test_handler_cache_returns_same_body():
    """Second call within TTL returns the same body without a new DAO call."""
    from pytheum.api import status as _status_mod
    _status_mod._cache = None
    call_count = 0

    class _CountingDao:
        async def fetch_venue_stats(self):
            nonlocal call_count
            call_count += 1
            return []

    dao = _CountingDao()
    await handle_status({}, dao=dao)
    await handle_status({}, dao=dao)
    assert call_count == 1, "cache should have prevented a second DAO call"
