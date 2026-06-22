"""GET /v1/markets/{ref}/core — lean single-market fetch (t_get_market)."""

from __future__ import annotations

from typing import Any

from pytheum.api.markets_get import _market_core, handle_market_get


class _FakeEquiv:
    pairs_loaded = 137434

    def __init__(self, pairs: list[dict[str, Any]] | None = None) -> None:
        self._pairs = pairs or []

    def lookup(self, ref: str) -> tuple[list[dict[str, Any]], str]:
        return (self._pairs, "pm_gamma_id" if self._pairs else "none")


class _FakeDao:
    def __init__(self, row: dict[str, Any] | None) -> None:
        self._row = row

    async def fetch_market(self, ref: str) -> dict[str, Any] | None:
        if self._row and ref == self._row.get("id"):
            return self._row
        return None


def test_market_core_shapes_found_and_absent() -> None:
    absent = _market_core(None, ref="kalshi:KXX", venue="kalshi")
    assert absent == {"id": "kalshi:KXX", "venue": "kalshi", "question": None, "found": False}

    row = {"id": "kalshi:KXX", "venue": "kalshi", "question": "Will X?",
           "status": "active", "volume_usd": 1234.0, "resolution_at": None,
           "payload": {"bestBid": 0.40, "bestAsk": 0.42}}
    core = _market_core(row, ref="kalshi:KXX")
    assert core["found"] is True
    assert core["venue"] == "kalshi" and core["question"] == "Will X?"
    assert core["volume_usd"] == 1234.0
    assert core["book"] and core["book"]["bid"] == 0.40 and core["book"]["ask"] == 0.42


async def test_get_market_found_with_equivalent_flag() -> None:
    row = {"id": "polymarket:558936", "venue": "polymarket", "question": "Spain WC?",
           "status": "active", "volume_usd": 5000.0, "resolution_at": None,
           "payload": {"bestBid": 0.17, "bestAsk": 0.18}}
    dao = _FakeDao(row)
    equiv = _FakeEquiv(pairs=[{"kalshi_ref": "kalshi:KXWC"}])
    code, body = await handle_market_get("polymarket:558936", {}, dao=dao, equivalence=equiv)
    assert code == 200
    assert body["market"]["found"] is True
    assert body["meta"]["has_equivalent"] is True  # → drill into /equivalents
    assert body["meta"]["matched_via"] == "pm_gamma_id"


async def test_get_market_absent_degrades_not_errors() -> None:
    code, body = await handle_market_get(
        "polymarket:999999", {}, dao=_FakeDao(None), equivalence=_FakeEquiv())
    assert code == 200
    assert body["market"]["found"] is False
    assert body["meta"].get("degraded") is True


async def test_get_market_no_dao_degrades() -> None:
    # Offline path (no DAO): _hydrate swallows the AttributeError → found false.
    code, body = await handle_market_get("kalshi:KXX", {}, dao=None, equivalence=_FakeEquiv())
    assert code == 200
    assert body["market"]["found"] is False
