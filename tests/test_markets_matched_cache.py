"""Short-TTL param-keyed response cache for GET /v1/markets/matched.

A load test showed /v1/markets/matched is a concurrency cliff: p50
156ms -> 789ms -> 1978ms (p95 3710ms) as concurrency goes 1->10->25 — the same
uncached-DB pattern /screen and /equivalents had. Matched pairs are a slow-moving
cross-venue collection, so a 20s param-keyed response cache flattens the curve.

Mirrors tests/test_markets_screen_cache.py: a counting fake DAO (spy on
fetch_markets_by_ids) plus a fake EquivalenceIndex (browse() is a pure in-memory
scan, so the DAO's fetch is the work we're caching).
"""
from __future__ import annotations

from typing import Any

import pytheum.api.markets_matched as matched_mod
from pytheum.api.markets_matched import handle_markets_matched


class _FakeEquivalence:
    """Minimal duck-typed EquivalenceIndex: browse() returns canned rows."""

    BET_TYPE_GROUPS: dict[str, set[str]] = {}
    pairs_loaded = 1
    file_missing = False
    load_error = None

    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows
        self.bet_types_available = sorted({r.get("bet_type") for r in rows if r.get("bet_type")})

    def browse(self, **kwargs: Any) -> tuple[list[dict[str, Any]], int]:
        return list(self._rows), len(self._rows)


class _CountingDao:
    """DAO that counts fetch_markets_by_ids invocations (cache hit -> no bump)."""

    def __init__(self, rows: list[dict[str, Any]] | None = None) -> None:
        self._rows = rows or []
        self.calls = 0

    async def fetch_markets_by_ids(self, ids: list[str]) -> list[dict[str, Any]]:
        self.calls += 1
        return list(self._rows)


def _pair(
    k_ref: str = "kalshi:KX-A",
    pm_ref: str = "polymarket:1",
    bet_type: str = "moneyline",
    **over: Any,
) -> dict[str, Any]:
    row: dict[str, Any] = {
        "kalshi_ref": k_ref,
        "kalshi_ticker": k_ref.split(":", 1)[-1],
        "pm_ref": pm_ref,
        "bet_type": bet_type,
        "method": "structured_key",
        "confidence": 1.0,
        "kalshi_title": over.pop("kalshi_title", f"K {k_ref}"),
        "pm_title": over.pop("pm_title", f"P {pm_ref}"),
    }
    row.update(over)
    return row


def _market(mid: str, **over: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "id": mid,
        "question": over.pop("question", f"q-{mid}"),
        "venue": over.pop("venue", "kalshi" if mid.startswith("kalshi:") else "polymarket"),
        "status": over.pop("status", "active"),
        "volume_usd": over.pop("volume_usd", 1000.0),
        "url": None,
        "payload": over.pop("payload", {"outcomePrices": "[0.6, 0.4]"}),
    }
    row.update(over)
    return row


def _clear_cache() -> None:
    matched_mod._matched_cache.clear()


# --------------------------------------------------------------------------- #
# (a) second identical call within TTL does NOT re-query the dao
# --------------------------------------------------------------------------- #


async def test_identical_call_hits_cache_no_requery() -> None:
    _clear_cache()
    eq = _FakeEquivalence([_pair()])
    dao = _CountingDao([_market("kalshi:KX-A"), _market("polymarket:1")])
    status1, body1 = await handle_markets_matched(
        {"limit": "10"}, dao=dao, equivalence=eq
    )
    assert status1 == 200
    assert dao.calls == 1

    status2, body2 = await handle_markets_matched(
        {"limit": "10"}, dao=dao, equivalence=eq
    )
    assert status2 == 200
    # No re-query: the dao was hit exactly once across both calls.
    assert dao.calls == 1
    # Cached body is byte-identical to the live body.
    assert body2 == body1


# --------------------------------------------------------------------------- #
# (b) different params -> separate cache entry (dao re-queried)
# --------------------------------------------------------------------------- #


async def test_different_limit_misses_cache() -> None:
    _clear_cache()
    eq = _FakeEquivalence([_pair()])
    dao = _CountingDao([_market("kalshi:KX-A"), _market("polymarket:1")])
    await handle_markets_matched({"limit": "10"}, dao=dao, equivalence=eq)
    assert dao.calls == 1
    await handle_markets_matched({"limit": "20"}, dao=dao, equivalence=eq)
    assert dao.calls == 2


async def test_different_params_miss_cache() -> None:
    _clear_cache()
    eq = _FakeEquivalence([_pair()])
    dao = _CountingDao([_market("kalshi:KX-A"), _market("polymarket:1")])
    await handle_markets_matched({}, dao=dao, equivalence=eq)
    assert dao.calls == 1
    await handle_markets_matched({"offset": "10"}, dao=dao, equivalence=eq)
    assert dao.calls == 2
    await handle_markets_matched({"sort_by": "confidence"}, dao=dao, equivalence=eq)
    assert dao.calls == 3
    await handle_markets_matched({"min_volume": "500"}, dao=dao, equivalence=eq)
    assert dao.calls == 4
    await handle_markets_matched({"fungible_only": "true"}, dao=dao, equivalence=eq)
    assert dao.calls == 5
    await handle_markets_matched({"bet_type": "moneyline"}, dao=dao, equivalence=eq)
    assert dao.calls == 6
    await handle_markets_matched({"q": "lakers"}, dao=dao, equivalence=eq)
    assert dao.calls == 7
    await handle_markets_matched({"league": "NBA"}, dao=dao, equivalence=eq)
    assert dao.calls == 8
    await handle_markets_matched({"date": "2026-07-01"}, dao=dao, equivalence=eq)
    assert dao.calls == 9


async def test_bet_type_aliases_share_cache_entry() -> None:
    """`bet_type` and `bet_types` collapse to the same normalized filter -> hit."""
    _clear_cache()
    eq = _FakeEquivalence([_pair()])
    dao = _CountingDao([_market("kalshi:KX-A"), _market("polymarket:1")])
    _, body_a = await handle_markets_matched(
        {"bet_type": "moneyline"}, dao=dao, equivalence=eq
    )
    assert dao.calls == 1
    _, body_b = await handle_markets_matched(
        {"bet_types": "moneyline"}, dao=dao, equivalence=eq
    )
    assert dao.calls == 1
    assert body_b == body_a


# --------------------------------------------------------------------------- #
# (c) force_refresh bypasses the cache
# --------------------------------------------------------------------------- #


async def test_force_refresh_bypasses_cache() -> None:
    _clear_cache()
    eq = _FakeEquivalence([_pair()])
    dao = _CountingDao([_market("kalshi:KX-A"), _market("polymarket:1")])
    await handle_markets_matched({"limit": "10"}, dao=dao, equivalence=eq)
    assert dao.calls == 1
    await handle_markets_matched(
        {"limit": "10"}, dao=dao, equivalence=eq, force_refresh=True
    )
    assert dao.calls == 2


# --------------------------------------------------------------------------- #
# (d) degraded equivalence (file_missing / load_error) is NOT cached
# --------------------------------------------------------------------------- #


async def test_degraded_equivalence_not_cached() -> None:
    _clear_cache()
    eq = _FakeEquivalence([])
    eq.file_missing = True
    dao = _CountingDao([])
    status, body = await handle_markets_matched({}, dao=dao, equivalence=eq)
    assert status == 200
    assert body["meta"]["degraded"] is True

    # Nothing cached for the degraded path: a healthy index re-runs the work.
    eq2 = _FakeEquivalence([_pair()])
    dao2 = _CountingDao([_market("kalshi:KX-A"), _market("polymarket:1")])
    await handle_markets_matched({}, dao=dao2, equivalence=eq2)
    assert dao2.calls == 1
