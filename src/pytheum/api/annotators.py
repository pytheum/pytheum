"""Serve-safe row-mutation annotators (quote staleness, moves, cross-venue).

No embedding / rolling_index / PIT imports. Safe to import from pytheum-serve.
"""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

# Quote-staleness: a "parked wall" = a quote frozen for a long time behind a tight
# spread on a still-active market — a resting limit order, not a live price. Shared
# by /screen (id-keyed rows) and /context sibling_markets (market_id-keyed) so an
# agent never ranks a frozen ghost as a live edge without a follow-up history call.
_PARKED_AFTER_S = 6 * 3600
_PARKED_MAX_SPREAD = 0.01


async def attach_quote_staleness(
    rows: list[dict[str, Any]], *, dao: Any, id_key: str = "id"
) -> None:
    """Mutate rows to add `last_move_age_s` (seconds since implied_yes last CHANGED,
    via the history endpoint's run-start definition — DAO.fetch_last_move_at, NOT a
    naive MAX(observed_at) which is last-OBSERVED) and `is_parked_wall` (long-frozen
    + tight-spread + active). ONE batched query for the whole list. Fully guarded:
    no-op if the dao lacks the method (minimal test daos) and never raises."""
    fetch = getattr(dao, "fetch_last_move_at", None)
    if fetch is None:
        return
    ids = [r[id_key] for r in rows if r.get(id_key)]
    if not ids:
        return
    try:
        last_move = await fetch(ids)
    except Exception:  # enrichment must never break the response
        return
    now = datetime.now(UTC)
    for r in rows:
        lm = last_move.get(r.get(id_key))
        if lm is None:
            continue
        if lm.tzinfo is None:
            lm = lm.replace(tzinfo=UTC)
        age = (now - lm).total_seconds()
        r["last_move_age_s"] = round(age)
        spread = (r.get("book") or {}).get("spread")
        r["is_parked_wall"] = bool(
            age > _PARKED_AFTER_S
            and isinstance(spread, (int, float)) and spread <= _PARKED_MAX_SPREAD
            and (r.get("status") or "").lower() == "active")


async def attach_moves(
    rows: list[dict[str, Any]], *, dao: Any, id_key: str = "id"
) -> None:
    """Mutate rows to add `move_24h`/`move_7d` (#265/#214) — one batched DAO
    call (live tape refs + #262 archive fallback, so 7d works beyond tape
    depth). Guarded: no-op if the dao lacks the method; never raises."""
    fetch = getattr(dao, "fetch_moves", None)
    if fetch is None:
        return
    ids = [r[id_key] for r in rows if r.get(id_key)]
    if not ids:
        return
    try:
        moves = await fetch(ids)
    except Exception:  # enrichment must never break the response
        return
    for r in rows:
        mv = moves.get(r.get(id_key))
        if mv:
            r.update(mv)


async def attach_cross_venue(
    rows: list[dict[str, Any]], *, dao: Any, id_key: str = "id"
) -> None:
    """Mutate rows to add `cross_venue` — the market's VERIFIED twin on the
    other venue from the matcher gold set ({market_id, method, confidence};
    pre-decided pairs served read-only, we do not match here). ONE batched
    query for the whole list. Fully guarded: no-op if the dao lacks the
    method (minimal test daos) and never raises."""
    fetch = getattr(dao, "fetch_equivalents_for_ids", None)
    if fetch is None:
        return
    ids = [r[id_key] for r in rows if r.get(id_key)]
    if not ids:
        return
    try:
        twins = await fetch(ids)
    except Exception:  # enrichment must never break the response
        return
    for r in rows:
        twin = twins.get(r.get(id_key))
        if twin:
            r["cross_venue"] = twin


def reserve_cross_venue_slot(
    rows: list[dict[str, Any]], *, limit: int
) -> list[dict[str, Any]]:
    """Force venue diversity in top-K by reserving slots for missing venues.

    Polymarket dominates similarity scores because its titles ARE the question
    text, while Kalshi titles are ticker codes. Without this, queries that
    semantically span multiple venues (Fed decisions, Bitcoin price, election
    odds) return top-K dominated by Polymarket and consumers never see the
    cross-venue alternatives.

    Algorithm: take rows pre-sorted desc by similarity. For each venue that
    has rows in the candidate pool but ISN'T in top-K, swap its best row
    into the bottom of top-K (displacing the lowest-ranked existing entry).
    Caps swaps at `limit - 1` so the top spot is always natural.

    No-op when: limit<=1, fewer than `limit` rows available, or no missing
    venues to add.
    """
    if limit < 2 or len(rows) <= limit:
        return rows
    top = rows[:limit]
    represented = {r.get("venue") for r in top if r.get("venue")}
    # Find best row from each MISSING venue (rows[limit:] already sim-desc)
    missing_best: list[dict[str, Any]] = []
    seen: set[str] = set(v for v in represented if v)
    for r in rows[limit:]:
        v = r.get("venue")
        if v and v not in seen:
            missing_best.append(r)
            seen.add(v)
    if not missing_best:
        return rows
    # Swap last n slots of top-K with the missing-venue best rows
    n_swap = min(len(missing_best), limit - 1)
    return [*top[: limit - n_swap], *missing_best[:n_swap]]
