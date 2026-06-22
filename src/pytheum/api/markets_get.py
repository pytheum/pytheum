"""GET /v1/markets/{ref}/core — lean single-market fetch.

The ergonomic "get one market by ref" tool: a trader lands with a venue id or a
market URL and wants that market's CORE (price / book / status / resolution)
*without* the heavy ``t_market_context`` payload (the probability ladder, sibling
markets, fetched news bodies). Accepts the same ref forms as the other ``{ref}``
endpoints: ``kalshi:<ticker>``, ``polymarket:<gamma_id|0xcond|slug>``, a raw
Kalshi ticker, or a market URL — ref inference is shared via ``normalize_ref``.

Also reports whether a verified cross-venue equivalent exists, so an agent knows
it can drill into ``/v1/markets/{ref}/equivalents`` for the twin + spread.
"""
from __future__ import annotations

from typing import Any

from pytheum.api.markets_equivalents import _hydrate
from pytheum.api.params import (
    book_from_payload,
    condition_id_from_payload,
    implied_yes_from_payload,
    resolution_status_from_payload,
)
from pytheum.api.ref_utils import normalize_ref


def _market_core(
    row: dict[str, Any] | None, *, ref: str, venue: str | None = None
) -> dict[str, Any]:
    """Shape a market row into the lean core. ``found=False`` when absent."""
    if row is None:
        return {"id": ref, "venue": venue, "question": None, "found": False}
    payload = row.get("payload")
    resolution_at = row.get("resolution_at")
    resolution_at_str: str | None = (
        resolution_at.isoformat()  # type: ignore[union-attr]
        if hasattr(resolution_at, "isoformat")
        else (str(resolution_at) if resolution_at is not None else None)
    )
    return {
        "id": row.get("id", ref),
        "venue": row.get("venue", venue),
        "question": row.get("question"),
        "status": row.get("status"),
        "implied_yes": implied_yes_from_payload(payload),
        "book": book_from_payload(payload),
        "volume_usd": row.get("volume_usd"),
        "condition_id": condition_id_from_payload(payload),
        "resolution_status": resolution_status_from_payload(payload),
        "resolution_at": resolution_at_str,
        "url": row.get("url"),
        "found": True,
    }


async def handle_market_get(
    ref: str,
    query: dict[str, str],
    *,
    dao: Any,
    equivalence: Any = None,
) -> tuple[int, dict[str, Any]]:
    """GET /v1/markets/{ref}/core handler. Degrades (found=false) rather than
    erroring when the market isn't in the store or no DAO is wired (offline)."""
    if equivalence is None:
        from pytheum.equivalence.index import get_index
        equivalence = get_index()

    ref_norm = normalize_ref(ref)

    venue: str | None = None
    if ":" in ref_norm:
        prefix = ref_norm.split(":", 1)[0].lower()
        if prefix in ("kalshi", "polymarket"):
            venue = prefix

    row = await _hydrate(ref_norm, dao)
    # Raw-ticker (no venue prefix) → try the kalshi-prefixed form.
    if row is None and ":" not in ref_norm:
        row = await _hydrate(f"kalshi:{ref_norm}", dao)
        if row is not None:
            venue = "kalshi"

    core = _market_core(row, ref=ref_norm, venue=venue)

    pairs, matched_via = equivalence.lookup(ref_norm)
    meta: dict[str, Any] = {
        "has_equivalent": bool(pairs),
        "matched_via": matched_via,
        "pairs_loaded": equivalence.pairs_loaded,
    }
    if row is None:
        meta["degraded"] = True
        meta["degraded_reason"] = "market_not_in_store"

    return 200, {"market": core, "meta": meta}
