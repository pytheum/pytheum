"""Fuse the /equivalents + /rules payloads into the MM reference record.

Pure: takes the two already-assembled endpoint dicts (oriented cross-venue prices +
book from equivalents; resolution text + resolution_at + method/confidence from rules)
and returns the market-maker reference record. No HTTP, no DAO — so the SAME assembly
serves both the REST handler (server-side) and any client composition. All orientation
(the Kalshi-YES side-map, the unoriented-pair veto) is already resolved upstream in the
equivalents handler's ``cross_venue`` block; here we only consume it.
"""
from __future__ import annotations

from typing import Any

from pytheum.mm.reference import Leg, advise


def _leg_from_book(venue: str, oriented_mid: Any, book: Any, *, allow_book_mid: bool) -> Leg:
    """Build a Leg in the COMMON (Kalshi-YES) frame.

    ``oriented_mid`` is the server-oriented implied_yes (already flipped into the
    Kalshi-YES frame when needed); ``book`` is that venue's RAW top-of-book. Implied is
    only ever derived from PM-style outcomePrices upstream, so a Kalshi leg often has a
    live book but no implied — hence ``allow_book_mid``: when the oriented implied is
    absent, fall back to the RAW book mid ONLY where the book's frame is unambiguous
    (the Kalshi leg, always native YES). The PM leg never falls back to its raw book
    (its YES/NO side-map is resolved upstream, not here), so we never guess its frame.

    When the oriented implied IS present, it is authoritative: we synthesise symmetric
    bid/ask around it (mid() stays exact) so the tightness/depth weight still sees the
    real spread width + top size (both orientation-invariant)."""
    bk = book if isinstance(book, dict) else {}
    bid, ask = bk.get("bid"), bk.get("ask")
    bsz, asz = bk.get("bid_size"), bk.get("ask_size")
    if isinstance(oriented_mid, (int, float)):
        spr = bk.get("spread")
        if spr is None and isinstance(bid, (int, float)) and isinstance(ask, (int, float)):
            spr = max(ask - bid, 0.0)
        if isinstance(spr, (int, float)) and spr >= 0:
            half = spr / 2.0
            return Leg(venue, implied_yes=oriented_mid, bid=oriented_mid - half,
                       ask=oriented_mid + half, bid_size=bsz, ask_size=asz)
        return Leg(venue, implied_yes=oriented_mid, bid_size=bsz, ask_size=asz)
    if allow_book_mid and isinstance(bid, (int, float)) and isinstance(ask, (int, float)):
        return Leg(venue, bid=bid, ask=ask, bid_size=bsz, ask_size=asz)  # native-frame book mid
    return Leg(venue)


def assemble_mm_reference(market_ref: str, equ: dict[str, Any],
                          rul: dict[str, Any]) -> dict[str, Any]:
    """Fuse the /equivalents (``equ``) + /rules (``rul``) payloads into the MM record."""
    cross = equ.get("cross_venue") or {}
    k_implied = cross.get("kalshi_implied")
    pm_implied = cross.get("pm_implied")
    # Orientation: the equivalents handler emits spread=None + spread_unavailable when the
    # pair has no verified side-map, meaning pm_implied is in its RAW frame and NOT safe to
    # blend against the Kalshi YES. Drop the PM leg from the reference in that case.
    orientation_unknown = bool(cross.get("spread_unavailable")) or (
        pm_implied is not None and k_implied is not None and cross.get("spread") is None)
    if orientation_unknown:
        pm_implied = None

    focal = equ.get("market") or {}
    focal_venue = (focal.get("venue") or "").lower()
    equivalents = equ.get("equivalents") or []
    counterpart = next((e for e in equivalents if e.get("implied_yes") is not None), None) \
        or (equivalents[0] if equivalents else {})
    k_ref: str | None
    pm_ref: str | None
    if focal_venue == "polymarket":
        k_book, pm_book = counterpart.get("book"), focal.get("book")
        k_ref, pm_ref = counterpart.get("id"), market_ref
    else:  # kalshi focal (or unknown → treat focal as the kalshi side)
        k_book, pm_book = focal.get("book"), counterpart.get("book")
        k_ref, pm_ref = market_ref, counterpart.get("id")

    # Kalshi book is native YES → safe implied fallback. PM book frame is resolved
    # upstream (the side-map), never here → no raw-book fallback for the PM leg.
    k_leg = _leg_from_book("kalshi", k_implied, k_book, allow_book_mid=True)
    pm_leg = _leg_from_book("polymarket", pm_implied, pm_book, allow_book_mid=False)

    # Rules / resolution / method / confidence from the /rules payload.
    r_market = rul.get("market") or {}
    r_equiv = rul.get("equivalent") or {}
    comparison = rul.get("comparison") or {}
    by_venue: dict[str, dict[str, Any]] = {}
    for blk in (r_market, r_equiv):
        v = (blk.get("venue") or "").lower()
        if v:
            by_venue[v] = blk
    k_rules = (by_venue.get("kalshi") or {}).get("resolution")
    pm_rules = (by_venue.get("polymarket") or {}).get("resolution")
    resolution_at = r_market.get("resolution_at") or r_equiv.get("resolution_at")
    method = comparison.get("method")
    confidence = comparison.get("confidence")

    ref = advise(k_leg, pm_leg, resolution_at=resolution_at, method=method,
                 confidence=confidence, kalshi_rules=k_rules, pm_rules=pm_rules)
    if orientation_unknown:
        ref["warnings"].insert(0, "orientation_unknown: pair lacks a verified side-map — "
                               "PM leg dropped from the reference; use t_find_divergences")
    same_day = comparison.get("same_deadline_day")
    if same_day is False:
        ref["warnings"].append("deadline_mismatch: the two legs resolve on different days")

    e_meta = equ.get("meta") or {}
    return {
        "mm_reference": ref,
        "pair": {
            "kalshi": {"ref": k_ref, "implied_yes": k_leg.mid()},
            "polymarket": {"ref": pm_ref, "implied_yes": pm_leg.mid()},
            "method": method,
            "confidence": confidence,
            "orientation": "unknown" if orientation_unknown else "known",
        },
        "meta": {
            "pairs_loaded": e_meta.get("pairs_loaded"),
            "dataset_version": e_meta.get("dataset_version"),
            "matched_via": e_meta.get("matched_via"),
            "same_deadline_day": same_day,
        },
    }
