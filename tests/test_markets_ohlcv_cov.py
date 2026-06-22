"""Coverage tests for pytheum.api.markets_ohlcv.handle_market_ohlcv.

Covers the param helpers (_parse_interval, _parse_ts), the validation/degraded
paths (invalid interval, invalid range), the happy path through an injected
fake OhlcvProvider, the legacy lazy-provider path (provider=None builds a
VenueFallbackOhlcv from clients), market metadata via an injected dao, and the
limit clamp.

No network: the provider is a pure in-memory fake; the lazy path uses a
clients stub whose venue is unreachable (kalshi:… with no client) — the
VenueFallbackOhlcv simply returns no bars rather than hitting a socket.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from pytheum.api import markets_ohlcv
from pytheum.api.markets_ohlcv import (
    _parse_interval,
    _parse_ts,
    handle_market_ohlcv,
)
from pytheum.ohlcv.provider import OhlcvProvider, OhlcvResult
from pytheum.trader.cache import SingleFlightCache

# ─────────────────────────────────────────────────────────────────────────────
# Fakes
# ─────────────────────────────────────────────────────────────────────────────


class _FakeProvider(OhlcvProvider):
    """Records the args get_bars was called with and returns a canned result."""

    def __init__(self) -> None:
        self.calls: list[tuple[Any, ...]] = []

    async def get_bars(
        self, ref: str, interval: str, since: datetime, until: datetime, limit: int
    ) -> OhlcvResult:
        self.calls.append((ref, interval, since, until, limit))
        bars = [{"t": "2026-06-01T00:00:00Z", "o": 0.5, "h": 0.6,
                 "l": 0.4, "c": 0.55, "v": 3}]
        return OhlcvResult(bars=bars, source="venue_live", partial_last_bucket=True)

    async def available_since(self, ref: str) -> datetime | None:
        return None


class _FakeDao:
    def __init__(self, meta: dict[str, Any] | None = None,
                 raise_exc: bool = False) -> None:
        self._meta = meta
        self._raise = raise_exc
        self.calls: list[str] = []

    async def fetch_market(self, ref: str) -> dict[str, Any] | None:
        self.calls.append(ref)
        if self._raise:
            raise RuntimeError("dao boom")
        return self._meta


# ─────────────────────────────────────────────────────────────────────────────
# _parse_interval
# ─────────────────────────────────────────────────────────────────────────────


def test_parse_interval_default_is_1h() -> None:
    assert _parse_interval(None) == ("1h", 3600)


def test_parse_interval_known_label() -> None:
    label, secs = _parse_interval("5m")  # type: ignore[misc]
    assert label == "5m"
    assert secs == 300


def test_parse_interval_case_insensitive() -> None:
    assert _parse_interval("1D")[0] == "1d"  # type: ignore[index]


def test_parse_interval_unknown_returns_none() -> None:
    assert _parse_interval("3w") is None


# ─────────────────────────────────────────────────────────────────────────────
# _parse_ts
# ─────────────────────────────────────────────────────────────────────────────


def test_parse_ts_none_returns_default() -> None:
    default = datetime(2026, 1, 1, tzinfo=UTC)
    assert _parse_ts(None, default=default) == default


def test_parse_ts_unix_seconds() -> None:
    default = datetime(2026, 1, 1, tzinfo=UTC)
    out = _parse_ts("1700000000", default=default)
    assert out.year == 2023  # 2023-11-14ish, not the default


def test_parse_ts_iso_with_z() -> None:
    default = datetime(2026, 1, 1, tzinfo=UTC)
    out = _parse_ts("2026-06-01T00:00:00Z", default=default)
    assert out == datetime(2026, 6, 1, tzinfo=UTC)


def test_parse_ts_iso_naive_gets_utc() -> None:
    default = datetime(2026, 1, 1, tzinfo=UTC)
    out = _parse_ts("2026-06-01T12:00:00", default=default)
    assert out.tzinfo == UTC
    assert out.hour == 12


def test_parse_ts_garbage_returns_default() -> None:
    default = datetime(2026, 1, 1, tzinfo=UTC)
    assert _parse_ts("not-a-date", default=default) == default


# ─────────────────────────────────────────────────────────────────────────────
# handle_market_ohlcv — validation / degraded
# ─────────────────────────────────────────────────────────────────────────────


async def test_invalid_interval_degrades() -> None:
    status, body = await handle_market_ohlcv(
        "kalshi:KX", {"interval": "3w"}, provider=_FakeProvider()
    )
    assert status == 200
    assert body["error"] == "invalid_interval"
    assert "3w" in body["hint"]


async def test_invalid_range_since_after_until_degrades() -> None:
    now = datetime.now(UTC)
    since = (now - timedelta(hours=1)).isoformat()
    until = (now - timedelta(hours=2)).isoformat()  # until < since
    status, body = await handle_market_ohlcv(
        "kalshi:KX", {"since": since, "until": until}, provider=_FakeProvider()
    )
    assert status == 200
    assert body["error"] == "invalid_range"


async def test_until_clamped_to_now(monkeypatch: pytest.MonkeyPatch) -> None:
    prov = _FakeProvider()
    # until far in the future must be clamped to now (no future buckets)
    future = (datetime.now(UTC) + timedelta(days=30)).isoformat()
    status, body = await handle_market_ohlcv(
        "kalshi:KX", {"until": future}, provider=prov
    )
    assert status == 200
    # provider was called with an until <= now
    _ref, _iv, _since, until_arg, _limit = prov.calls[0]
    assert until_arg <= datetime.now(UTC) + timedelta(seconds=1)


# ─────────────────────────────────────────────────────────────────────────────
# handle_market_ohlcv — happy path
# ─────────────────────────────────────────────────────────────────────────────


async def test_happy_path_with_injected_provider() -> None:
    prov = _FakeProvider()
    status, body = await handle_market_ohlcv(
        "kalshi:KXTEST", {"interval": "1h"}, provider=prov
    )
    assert status == 200
    assert body["interval"] == "1h"
    assert body["market"]["id"] == "kalshi:KXTEST"
    assert body["market"]["venue"] == "kalshi"
    assert body["meta"]["source"] == "venue_live"
    assert body["meta"]["count"] == 1
    assert body["meta"]["partial_last_bucket"] is True
    assert len(prov.calls) == 1


async def test_market_metadata_from_dao() -> None:
    prov = _FakeProvider()
    dao = _FakeDao(meta={"question": "Will X happen?"})
    status, body = await handle_market_ohlcv(
        "polymarket:some-slug", {}, provider=prov, dao=dao
    )
    assert status == 200
    assert body["market"]["question"] == "Will X happen?"
    assert body["market"]["venue"] == "polymarket"
    assert dao.calls == ["polymarket:some-slug"]


async def test_dao_exception_suppressed_question_none() -> None:
    prov = _FakeProvider()
    dao = _FakeDao(raise_exc=True)
    status, body = await handle_market_ohlcv(
        "kalshi:KX", {}, provider=prov, dao=dao
    )
    assert status == 200
    # dao raised -> market_meta stays None, question is None, no 500
    assert body["market"]["question"] is None


async def test_bare_ref_has_empty_venue() -> None:
    prov = _FakeProvider()
    status, body = await handle_market_ohlcv("bareref", {}, provider=prov)
    assert status == 200
    # no ':' -> venue resolves to None in the market block
    assert body["market"]["venue"] is None


async def test_limit_clamped_to_max() -> None:
    prov = _FakeProvider()
    await handle_market_ohlcv("kalshi:KX", {"limit": "999999"}, provider=prov)
    assert prov.calls[0][4] == 1000  # clamped to max


async def test_limit_invalid_falls_back_to_default() -> None:
    prov = _FakeProvider()
    await handle_market_ohlcv("kalshi:KX", {"limit": "abc"}, provider=prov)
    assert prov.calls[0][4] == 500  # default


async def test_limit_below_one_clamped_to_one() -> None:
    prov = _FakeProvider()
    await handle_market_ohlcv("kalshi:KX", {"limit": "0"}, provider=prov)
    assert prov.calls[0][4] == 1


# ─────────────────────────────────────────────────────────────────────────────
# handle_market_ohlcv — legacy lazy-provider path (provider=None)
# ─────────────────────────────────────────────────────────────────────────────


async def test_lazy_provider_built_from_clients_no_kalshi_client() -> None:
    """provider=None -> VenueFallbackOhlcv(clients, cache) is constructed.

    With a clients stub whose .kalshi is None, the fallback provider returns no
    bars (degraded) rather than touching a socket. Exercises the lazy-build
    branch (line ~159) end-to-end.
    """
    class _C:
        kalshi = None
        polymarket = None

    cache = SingleFlightCache()
    status, body = await handle_market_ohlcv(
        "kalshi:KXTEST", {"interval": "1h"}, dao=None, clients=_C(), _cache=cache
    )
    assert status == 200
    assert body["interval"] == "1h"
    # venue_live source from the fallback provider, empty candle set
    assert body["meta"]["source"] == "venue_live"
    assert body["meta"]["count"] == 0


def test_module_reexports_present() -> None:
    # backwards-compat re-exports
    assert hasattr(markets_ohlcv, "resample_to_ohlcv")
    assert hasattr(markets_ohlcv, "_parse_kalshi_candles")
