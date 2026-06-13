"""GET /v1/markets/{ref}/related — correlated cross-venue markets.

Serves the matcher's RELATED tier: pairs that track the same asset/event but
are NOT settlement-equivalent (different bands, sources, or deadlines). This
is hedge-discovery data — each row carries the relation type, both venues'
bands, and a basis note explaining exactly how settlement differs. Use
/equivalents for true same-market pairs.
"""
from __future__ import annotations

from typing import Any

from pytheum.api.markets_equivalents import (
    _MATCHED_VIA_VENUE,
    _hydrate,
    _minimal_market_block,
    _row_to_market_block,
)
from pytheum.api.ref_utils import normalize_ref


def _build_related_item(
    row: dict[str, Any],
    *,
    counterpart_ref: str,
    counterpart_venue: str,
    counterpart_row: dict[str, Any] | None,
) -> dict[str, Any]:
    item: dict[str, Any] = {
        "id": counterpart_ref,
        "venue": counterpart_venue,
        "question": (
            row.get("pm_title") if counterpart_venue == "polymarket"
            else row.get("kalshi_title")
        ),
        "relation": row.get("relation"),
        "asset": row.get("asset"),
        "date": row.get("date"),
        "kalshi_band": row.get("kalshi_band"),
        "pm_band": row.get("pm_band"),
        "basis_note": row.get("basis_note"),
        "implied_yes": None,
        "book": None,
        "volume_usd": None,
        "url": None,
    }
    if counterpart_venue == "polymarket" and row.get("pm_condition_id"):
        item["condition_id"] = row["pm_condition_id"]
    if counterpart_row is not None:
        hydrated = _row_to_market_block(counterpart_row, counterpart_ref)
        for key in ("question", "implied_yes", "book", "volume_usd", "url", "status"):
            if hydrated.get(key) is not None:
                item[key] = hydrated[key]
    return item


async def handle_market_related(
    ref: str,
    query: dict[str, str],
    *,
    dao: Any,
    related: Any = None,
) -> tuple[int, dict[str, Any]]:
    """GET /v1/markets/{ref}/related handler (never-500 convention)."""
    if related is None:
        from pytheum.related.index import get_index
        related = get_index()

    ref_norm = normalize_ref(ref)

    rows, matched_via = related.lookup(ref_norm)

    focal_venue: str | None = _MATCHED_VIA_VENUE.get(matched_via)
    if focal_venue is None and ":" in ref_norm:
        prefix = ref_norm.split(":", 1)[0].lower()
        if prefix in ("kalshi", "polymarket"):
            focal_venue = prefix

    focal_row = await _hydrate(ref_norm, dao)
    if focal_row is None and rows and matched_via in ("pm_condition_id", "pm_slug"):
        canonical = rows[0].get("pm_ref")
        if canonical and canonical != ref_norm:
            focal_row = await _hydrate(canonical, dao)
    if focal_row is None and rows and matched_via == "kalshi_ticker" and ":" not in ref_norm:
        focal_row = await _hydrate(f"kalshi:{ref_norm}", dao)

    focal_question: str | None = None
    if rows:
        focal_question = (
            rows[0].get("kalshi_title") if focal_venue == "kalshi"
            else rows[0].get("pm_title")
        )
    market_block = (
        _row_to_market_block(focal_row, ref_norm)
        if focal_row is not None
        else _minimal_market_block(ref_norm, question=focal_question, venue=focal_venue)
    )

    if focal_venue == "kalshi":
        counterpart_venue, counterpart_ref_key = "polymarket", "pm_ref"
    elif focal_venue == "polymarket":
        counterpart_venue, counterpart_ref_key = "kalshi", "kalshi_ref"
    else:
        counterpart_venue, counterpart_ref_key = "", ""

    related_items: list[dict[str, Any]] = []
    for row in rows:
        counterpart_ref = row.get(counterpart_ref_key, "") if counterpart_ref_key else ""
        if not counterpart_ref:
            continue
        counterpart_row = await _hydrate(counterpart_ref, dao)
        related_items.append(
            _build_related_item(
                row,
                counterpart_ref=counterpart_ref,
                counterpart_venue=counterpart_venue,
                counterpart_row=counterpart_row,
            )
        )

    meta: dict[str, Any] = {
        "pairs_loaded": related.pairs_loaded,
        "matched_via": matched_via,
    }
    if getattr(related, "dataset_version", None):
        meta["dataset_version"] = related.dataset_version
    if getattr(related, "file_missing", False):
        meta["degraded"] = "related dataset file missing"

    return 200, {"market": market_block, "related": related_items, "meta": meta}
