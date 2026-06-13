"""GET /v1/markets/{ref}/rules — settlement-semantics comparison.

Returns the full resolution rules text for a focal market AND its verified
cross-venue equivalent side by side, with deadline comparison — so an agent can
check whether two venues' identical-seeming markets actually resolve the same way
before treating their prices as comparable.

Accepts:
  kalshi:<ticker>           e.g. kalshi:KXCOLOMBIAPRES-26-VDAV
  polymarket:<gamma_id>     e.g. polymarket:569356
  polymarket:0x<cond_id>   e.g. polymarket:0x849a3e...
  polymarket:<slug>         e.g. polymarket:will-vicky-dvila-...
  <raw ticker>              e.g. KXCOLOMBIAPRES-26-VDAV  (no venue prefix)
"""
from __future__ import annotations

from typing import Any

from pytheum.api.markets_equivalents import _MATCHED_VIA_VENUE, _hydrate
from pytheum.api.params import resolution_from_payload
from pytheum.api.ref_utils import normalize_ref


def _market_rules_block(
    row: dict[str, Any] | None,
    *,
    ref: str,
    question: str | None = None,
    venue: str | None = None,
) -> dict[str, Any]:
    """Build a market block for the /rules endpoint.

    When the market is in the store the block is fully hydrated.  When it is
    absent from the store but known to the equivalence index the block carries
    only the index titles and null for rules/deadline/url.
    """
    if row is None:
        return {
            "id": ref,
            "venue": venue,
            "question": question,
            "resolution": None,
            "resolution_at": None,
            "url": None,
        }
    resolution_at = row.get("resolution_at")
    resolution_at_str: str | None = (
        resolution_at.isoformat()  # type: ignore[union-attr]
        if hasattr(resolution_at, "isoformat")
        else (str(resolution_at) if resolution_at is not None else None)
    )
    return {
        "id": row.get("id", ref),
        "venue": row.get("venue", venue),
        "question": row.get("question") or question,
        "resolution": resolution_from_payload(row.get("payload")),
        "resolution_at": resolution_at_str,
        "url": row.get("url"),
    }


def _compare_deadlines(
    focal_block: dict[str, Any],
    equiv_block: dict[str, Any] | None,
    *,
    focal_venue: str | None,
    pair: dict[str, Any] | None,
) -> dict[str, Any]:
    """Build the comparison block for the /rules response.

    Compares resolution deadlines at day granularity (venues record the same
    event at different intraday times; only the calendar day matters for
    settlement semantics).
    """
    kalshi_deadline: str | None = None
    pm_deadline: str | None = None

    if focal_venue == "kalshi":
        kalshi_deadline = focal_block.get("resolution_at")
        if equiv_block is not None:
            pm_deadline = equiv_block.get("resolution_at")
    elif focal_venue == "polymarket":
        pm_deadline = focal_block.get("resolution_at")
        if equiv_block is not None:
            kalshi_deadline = equiv_block.get("resolution_at")

    same_day: bool | None = None
    if kalshi_deadline and pm_deadline:
        try:
            same_day = kalshi_deadline[:10] == pm_deadline[:10]
        except (TypeError, IndexError):
            same_day = None

    return {
        "deadlines": {"kalshi": kalshi_deadline, "polymarket": pm_deadline},
        "same_deadline_day": same_day,
        "confidence": pair.get("confidence") if pair else None,
        "method": pair.get("method") if pair else None,
    }


async def handle_market_rules(
    ref: str,
    query: dict[str, str],
    *,
    dao: Any,
    equivalence: Any = None,
) -> tuple[int, dict[str, Any]]:
    """GET /v1/markets/{ref}/rules handler.

    `equivalence` accepts an EquivalenceIndex (or duck-typed equivalent).
    Defaults to the module-level singleton (lazy-loaded on first call).
    """
    if equivalence is None:
        from pytheum.equivalence.index import get_index
        equivalence = get_index()

    # Normalise ref: URL extraction + venue-prefix case-fold.
    ref_norm = normalize_ref(ref)

    # Resolve equivalence pairs
    pairs, matched_via = equivalence.lookup(ref_norm)

    # Determine focal venue from matched_via (most reliable) or the ref prefix
    focal_venue: str | None = _MATCHED_VIA_VENUE.get(matched_via)
    if focal_venue is None and ":" in ref_norm:
        prefix = ref_norm.split(":", 1)[0].lower()
        if prefix in ("kalshi", "polymarket"):
            focal_venue = prefix

    # Hydrate focal market from store
    focal_row = await _hydrate(ref_norm, dao)

    # Same fallbacks as handle_market_equivalents
    if focal_row is None and pairs and matched_via in ("pm_condition_id", "pm_slug"):
        canonical = pairs[0].get("pm_ref")
        if canonical and canonical != ref_norm:
            focal_row = await _hydrate(canonical, dao)

    if focal_row is None and pairs and matched_via == "kalshi_ticker" and ":" not in ref_norm:
        focal_row = await _hydrate(f"kalshi:{ref_norm}", dao)

    # Focal question from export titles when row is absent
    focal_question: str | None = None
    if pairs:
        focal_question = (
            pairs[0].get("kalshi_title") if focal_venue == "kalshi"
            else pairs[0].get("pm_title")
        )

    focal_block = _market_rules_block(
        focal_row,
        ref=ref_norm,
        question=focal_question,
        venue=focal_venue,
    )

    # Determine the first (and expected only) counterpart
    first_pair = pairs[0] if pairs else None
    equiv_block: dict[str, Any] | None = None

    if first_pair is not None:
        if focal_venue == "kalshi":
            counterpart_venue = "polymarket"
            counterpart_ref = first_pair.get("pm_ref") or ""
            export_question = first_pair.get("pm_title")
        elif focal_venue == "polymarket":
            counterpart_venue = "kalshi"
            counterpart_ref = first_pair.get("kalshi_ref") or ""
            export_question = first_pair.get("kalshi_title")
        else:
            counterpart_venue = ""
            counterpart_ref = ""
            export_question = None

        if counterpart_ref:
            counterpart_row = await _hydrate(counterpart_ref, dao)
            equiv_block = _market_rules_block(
                counterpart_row,
                ref=counterpart_ref,
                question=(
                    (counterpart_row.get("question") or export_question)
                    if counterpart_row is not None
                    else export_question
                ),
                venue=counterpart_venue,
            )

    comparison = _compare_deadlines(
        focal_block,
        equiv_block,
        focal_venue=focal_venue,
        pair=first_pair,
    )

    # Meta block
    meta: dict[str, Any] = {
        "pairs_loaded": equivalence.pairs_loaded,
        "dataset_version": equivalence.dataset_version,
        "matched_via": matched_via,
    }
    if getattr(equivalence, "file_missing", False):
        meta["degraded"] = True
        meta["degraded_reason"] = "equivalence_file_not_found"
    elif getattr(equivalence, "load_error", None):
        meta["degraded"] = True
        meta["degraded_reason"] = equivalence.load_error

    return 200, {
        "market": focal_block,
        "equivalent": equiv_block,
        "comparison": comparison,
        "meta": meta,
    }
