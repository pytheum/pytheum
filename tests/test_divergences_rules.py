"""Tests for rules-bundled divergences (Item 3).

Covers:
1. _leg(r, include_rules=True) adds a ``resolution`` field.
2. _leg(r, include_rules=False) does NOT add a ``resolution`` field.
3. handle_markets_equivalents collection: legs carry ``resolution`` when
   include_rules=True.
"""
from __future__ import annotations

import pytest

from pytheum.api.markets_equivalents import _leg

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_market_row(
    *,
    ref: str = "polymarket:99",
    venue: str = "polymarket",
    description: str | None = None,
) -> dict:
    """Build a minimal market row as returned by the DAO."""
    payload: dict = {}
    if description is not None:
        payload["description"] = description
    return {
        "id": ref,
        "venue": venue,
        "question": "Will X happen?",
        "status": "active",
        "volume_usd": 1000.0,
        "liquidity_usd": 500.0,
        "url": "https://polymarket.com/event/x",
        "resolution_at": None,
        "payload": payload or None,
    }


# ---------------------------------------------------------------------------
# 1 & 2. _leg() include_rules flag
# ---------------------------------------------------------------------------


def test_leg_include_rules_true_adds_resolution_field():
    """_leg(r, include_rules=True) must add a 'resolution' key to the leg."""
    row = _make_market_row(description="This market resolves YES if …")
    leg = _leg(row, include_rules=True)
    assert "resolution" in leg
    assert leg["resolution"] == "This market resolves YES if …"


def test_leg_include_rules_false_omits_resolution_field():
    """_leg(r, include_rules=False) (the default) must NOT include 'resolution'."""
    row = _make_market_row(description="This market resolves YES if …")
    leg = _leg(row, include_rules=False)
    assert "resolution" not in leg


def test_leg_include_rules_default_omits_resolution():
    """The default for include_rules is False."""
    row = _make_market_row(description="Some resolution text")
    leg = _leg(row)
    assert "resolution" not in leg


def test_leg_include_rules_true_none_when_no_description():
    """When the payload has no description, resolution is None (not missing)."""
    row = _make_market_row(description=None)
    leg = _leg(row, include_rules=True)
    assert "resolution" in leg
    assert leg["resolution"] is None


def test_leg_include_rules_true_payload_none():
    """When the row has no payload at all, resolution is None."""
    row = _make_market_row()
    row["payload"] = None
    leg = _leg(row, include_rules=True)
    assert "resolution" in leg
    assert leg["resolution"] is None


# ---------------------------------------------------------------------------
# 3. handle_markets_equivalents collection — include_rules propagates to legs
# ---------------------------------------------------------------------------


class _SimpleDao:
    def __init__(self, store: dict | None = None) -> None:
        self._store: dict = store or {}

    async def fetch_market(self, ref: str) -> dict | None:
        return self._store.get(ref)

    async def fetch_markets_by_ids(self, ids: list[str]) -> list[dict]:
        return [self._store[ref] for ref in ids if ref in self._store]

    async def fetch_equivalence_pairs(self, limit: int = 50) -> list[dict]:
        return []


def _make_index_with_pair(
    *,
    k_ref: str = "kalshi:KX-TEST",
    k_ticker: str = "KX-TEST",
    pm_ref: str = "polymarket:77",
    pm_gid: str = "77",
    method: str = "structured_key",
) -> object:
    """Build an EquivalenceIndex-like object with one pair (no disk I/O)."""
    from pytheum.equivalence.index import EquivalenceIndex

    idx = EquivalenceIndex()
    idx.dataset_version = "2026-06-12T00:00:00Z"
    row = {
        "kalshi_ref": k_ref,
        "kalshi_ticker": k_ticker,
        "pm_ref": pm_ref,
        "pm_gamma_id": pm_gid,
        "bet_type": "event",
        "method": method,
        "confidence": 1.0,
        "kalshi_title": "Will X happen?",
        "pm_title": "Will X happen?",
    }
    idx._rows.append(row)
    idx._by_kalshi_ticker.setdefault(k_ticker, []).append(row)
    idx._by_pm_gamma_id.setdefault(pm_gid, []).append(row)
    return idx


@pytest.mark.asyncio
async def test_collection_endpoint_include_rules_true_legs_have_resolution():
    """GET /v1/markets/equivalents?include_rules=true — each leg carries resolution."""
    from pytheum.api.markets_equivalents import _cache, handle_markets_equivalents

    _cache.clear()

    k_row = _make_market_row(
        ref="kalshi:KX-TEST",
        venue="kalshi",
        description="Kalshi resolution clause: resolves YES if …",
    )
    pm_row = _make_market_row(
        ref="polymarket:77",
        venue="polymarket",
        description="Polymarket resolution clause: resolves YES if …",
    )
    dao = _SimpleDao({"kalshi:KX-TEST": k_row, "polymarket:77": pm_row})
    idx = _make_index_with_pair()

    status, body = await handle_markets_equivalents(
        {"include_rules": "true"},
        dao=dao,
        equivalence=idx,
        force_refresh=True,
    )
    assert status == 200
    assert body["meta"]["include_rules"] is True
    for pair in body.get("pairs", []):
        for leg_key in ("a", "b"):
            leg = pair.get(leg_key)
            if leg is not None:
                assert "resolution" in leg, (
                    f"Leg '{leg_key}' missing 'resolution' with include_rules=True"
                )


@pytest.mark.asyncio
async def test_collection_endpoint_include_rules_false_legs_no_resolution():
    """GET /v1/markets/equivalents — legs do NOT carry resolution by default."""
    from pytheum.api.markets_equivalents import _cache, handle_markets_equivalents

    _cache.clear()

    k_row = _make_market_row(
        ref="kalshi:KX-TEST",
        venue="kalshi",
        description="Some resolution text",
    )
    pm_row = _make_market_row(
        ref="polymarket:77",
        venue="polymarket",
        description="Some other resolution text",
    )
    dao = _SimpleDao({"kalshi:KX-TEST": k_row, "polymarket:77": pm_row})
    idx = _make_index_with_pair()

    _, body = await handle_markets_equivalents(
        {},
        dao=dao,
        equivalence=idx,
        force_refresh=True,
    )
    assert body["meta"]["include_rules"] is False
    for pair in body.get("pairs", []):
        for leg_key in ("a", "b"):
            leg = pair.get(leg_key)
            if leg is not None:
                assert "resolution" not in leg, (
                    f"Leg '{leg_key}' should not have 'resolution' when include_rules=False"
                )
