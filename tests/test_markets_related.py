"""Tests for GET /v1/markets/{ref}/related."""
from __future__ import annotations

import pytest

from pytheum.api.markets_related import handle_market_related
from pytheum.related.index import RelatedIndex

_ROW = {
    "kalshi_ref": "kalshi:KXBTC-26MAR07-T2.02",
    "kalshi_ticker": "KXBTC-26MAR07-T2.02",
    "kalshi_title": "XRP between $2.02 and $2.0399 at 1PM EDT?",
    "pm_ref": "polymarket:1066948",
    "pm_gamma_id": "1066948",
    "pm_condition_id": "0xdeadbeef",
    "pm_slug": "xrp-above-180-march-7",
    "pm_title": "XRP price on March 7?",
    "relation": "same_asset_date_contained",
    "asset": "XRP",
    "date": "2026-03-07",
    "kalshi_band": "$2.02 to 2.0399",
    "pm_band": ">1.80",
    "basis_note": "Kalshi: CF Benchmarks RTI 60s-avg; Polymarket: different source",
}


def _make_index(rows: list[dict] | None = None, *, file_missing: bool = False) -> RelatedIndex:
    idx = RelatedIndex()
    if file_missing:
        idx.file_missing = True
        return idx
    idx.dataset_version = "2026-06-12T00:00:00Z"
    for row in (rows or [_ROW]):
        idx._rows.append(row)
        kt = row.get("kalshi_ticker")
        if kt:
            idx._by_kalshi_ticker.setdefault(kt, []).append(row)
        gid = row.get("pm_gamma_id")
        if gid:
            idx._by_pm_gamma_id.setdefault(gid, []).append(row)
        cid = row.get("pm_condition_id")
        if cid:
            idx._by_pm_condition_id.setdefault(cid.lower(), []).append(row)
        slug = row.get("pm_slug")
        if slug:
            idx._by_pm_slug.setdefault(slug, []).append(row)
    return idx


class _SimpleDao:
    def __init__(self, store: dict | None = None) -> None:
        self._store: dict = store or {}

    async def fetch_market(self, ref: str) -> dict | None:
        return self._store.get(ref)


_PM_ROW = {
    "id": "polymarket:1066948",
    "question": "XRP price on March 7?",
    "venue": "polymarket",
    "status": "active",
    "volume_usd": 50000.0,
    "url": "https://polymarket.com/event/xrp-above-180-march-7",
    "resolution_at": None,
    "payload": {"outcomePrices": '["0.62", "0.38"]', "bestBid": "0.61", "bestAsk": "0.63"},
}


@pytest.mark.asyncio
async def test_related_by_kalshi_ref() -> None:
    status, body = await handle_market_related(
        "kalshi:KXBTC-26MAR07-T2.02", {}, dao=_SimpleDao({"polymarket:1066948": _PM_ROW}),
        related=_make_index(),
    )
    assert status == 200
    assert len(body["related"]) == 1
    item = body["related"][0]
    assert item["relation"] == "same_asset_date_contained"
    assert item["kalshi_band"] == "$2.02 to 2.0399"
    assert item["basis_note"].startswith("Kalshi: CF Benchmarks")
    assert item["implied_yes"] is not None  # hydrated from store
    assert body["meta"]["matched_via"] == "kalshi_ticker"
    assert body["meta"]["pairs_loaded"] == 1


@pytest.mark.asyncio
async def test_related_by_pm_gamma_and_slug() -> None:
    idx = _make_index()
    for ref, via in (("polymarket:1066948", "pm_gamma_id"), ("xrp-above-180-march-7", "pm_slug")):
        status, body = await handle_market_related(ref, {}, dao=_SimpleDao(), related=idx)
        assert status == 200
        assert body["meta"]["matched_via"] == via
        assert body["related"][0]["venue"] == "kalshi"
        assert body["related"][0]["id"] == "kalshi:KXBTC-26MAR07-T2.02"


@pytest.mark.asyncio
async def test_related_unknown_ref_empty_200() -> None:
    status, body = await handle_market_related(
        "kalshi:KXNOPE-26", {}, dao=_SimpleDao(), related=_make_index(),
    )
    assert status == 200
    assert body["related"] == []
    assert body["meta"]["matched_via"] == "none"


@pytest.mark.asyncio
async def test_related_missing_file_degrades() -> None:
    status, body = await handle_market_related(
        "kalshi:KXBTC-26MAR07-T2.02", {}, dao=_SimpleDao(),
        related=_make_index(file_missing=True),
    )
    assert status == 200
    assert body["related"] == []
    assert "degraded" in body["meta"]
