"""Regression tests for the /v1/markets/equivalents browse over-fetch.

The export is liveness-ordered (soonest-resolving first), so already-resolved
pairs cluster at the FRONT.  A live-trader probe found the browse returned an
empty page once the export aged a day: the old handler took the first `limit`
rows then dropped the stale ones, and when every soonest-first row had since
resolved, `pairs` came back empty.

The fix: skip row-level-stale rows up front (via the export's resolution_date)
while scanning a bounded budget, and over-fetch candidates so book-level stale
drops don't starve the page below `limit`.

Handlers are exercised directly with an in-memory index + fake DAO (no disk/DB).
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from pytheum.api.markets_equivalents import (
    _cache,
    _index_rows_to_pairs,
    handle_markets_equivalents,
)


def _iso(days_from_now: float) -> str:
    return (datetime.now(UTC) + timedelta(days=days_from_now)).isoformat()


def _row(n: int, *, days: float) -> dict:
    """An export row whose resolution_date is `days` from now."""
    return {
        "kalshi_ref": f"kalshi:K{n}",
        "kalshi_ticker": f"K{n}",
        "pm_ref": f"polymarket:{n}",
        "pm_gamma_id": str(n),
        "method": "structured_key",
        "confidence": 1.0,
        "bet_type": "moneyline",
        "resolution_date": _iso(days),
    }


class _HydratingDao:
    """fetch_markets_by_ids returns a market row per id; is_stale is derived
    by the handler from resolution_at (matching the export's resolution_date)."""

    def __init__(self, res_at: dict[str, str]) -> None:
        self._res_at = res_at  # id -> iso resolution_at

    async def fetch_markets_by_ids(self, ids: list[str]) -> list[dict]:
        out = []
        for i in ids:
            out.append({
                "id": i,
                "question": f"Q {i}",
                "venue": i.split(":")[0],
                "status": "active",
                "volume_usd": 1_000_000.0,
                "resolution_at": self._res_at.get(i),
                "payload": None,
            })
        return out

    async def fetch_equivalence_pairs(self, limit: int = 50) -> list[dict]:
        return []


def _make_index(rows: list[dict]):
    from pytheum.equivalence.index import EquivalenceIndex

    idx = EquivalenceIndex()
    idx.dataset_version = "2026-06-22T00:00:00Z"
    idx._rows.extend(rows)
    return idx


# ---------------------------------------------------------------------------
# _index_rows_to_pairs — row-level stale skip + scan budget
# ---------------------------------------------------------------------------


def test_index_rows_skips_resolved_front():
    """The resolved front (past resolution_date) is skipped; live rows surface."""
    rows = [_row(i, days=-5) for i in range(100)]          # all resolved
    rows += [_row(1000 + i, days=+10) for i in range(20)]  # live tail
    pairs = _index_rows_to_pairs(rows, limit=10)
    assert len(pairs) == 10
    assert all(p["kalshi_market_id"].startswith("kalshi:K10") for p in pairs)


def test_index_rows_scan_budget_caps_work():
    """An aged front larger than the scan budget yields no candidates (graceful)."""
    rows = [_row(i, days=-5) for i in range(50)] + [_row(9999, days=+10)]
    pairs = _index_rows_to_pairs(rows, limit=10, scan_budget=10)
    assert pairs == []  # the one live row sits past the 10-row budget


def test_index_rows_skip_row_stale_false_keeps_resolved():
    """skip_row_stale=False reverts to the old first-N behaviour (no DB filter)."""
    rows = [_row(i, days=-5) for i in range(5)]
    pairs = _index_rows_to_pairs(rows, limit=10, skip_row_stale=False)
    assert len(pairs) == 5


def test_index_rows_no_resolution_date_not_stale():
    """Rows without resolution_date are never row-stale (back-compat)."""
    rows = [{"kalshi_ref": "kalshi:K1", "pm_ref": "polymarket:1",
             "method": "structured_key"}]
    pairs = _index_rows_to_pairs(rows, limit=10)
    assert len(pairs) == 1


# ---------------------------------------------------------------------------
# handle_markets_equivalents — aged-front returns live pairs, not empty
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_handler_aged_front_still_returns_live_pairs():
    """The reported bug: aged export front all-resolved -> page must NOT be empty."""
    _cache.clear()
    resolved = [_row(i, days=-3) for i in range(80)]
    live = [_row(1000 + i, days=+14) for i in range(10)]
    idx = _make_index(resolved + live)
    res_at = {r["pm_ref"]: r["resolution_date"] for r in resolved + live}
    res_at.update({r["kalshi_ref"]: r["resolution_date"] for r in resolved + live})
    dao = _HydratingDao(res_at)

    status, body = await handle_markets_equivalents(
        {"limit": "5"}, dao=dao, equivalence=idx, force_refresh=True,
    )
    assert status == 200
    assert body["count"] == 5, "aged front must not produce an empty page"
    for p in body["pairs"]:
        assert p["a"]["is_stale"] is False
        assert p["b"]["is_stale"] is False


@pytest.mark.asyncio
async def test_handler_truncates_to_limit():
    """Over-fetch hydrates candidates but the page is capped at `limit`."""
    _cache.clear()
    live = [_row(1000 + i, days=+14) for i in range(40)]
    idx = _make_index(live)
    res_at = {r["pm_ref"]: r["resolution_date"] for r in live}
    res_at.update({r["kalshi_ref"]: r["resolution_date"] for r in live})
    dao = _HydratingDao(res_at)

    _, body = await handle_markets_equivalents(
        {"limit": "5"}, dao=dao, equivalence=idx, force_refresh=True,
    )
    assert body["count"] == 5
    assert body["meta"]["limit"] == 5
    # candidates_hydrated reflects the over-fetch (> page size).
    assert body["meta"]["candidates_hydrated"] >= 5
