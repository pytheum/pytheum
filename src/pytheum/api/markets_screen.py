"""GET /v1/markets/screen — structured (non-semantic) market discovery (t_screen).

Two trader-agents fired ~6 semantic searches + hand-filtered to do what one
structured screen should: "active markets, volume>$X, liquidity>$Y, resolving
before D, sorted by volume". This is that screen. Rows use the same fat shape as
/relevant-to (book/liquidity/resolution snippet/event_key) so the agent can
triage edge + tradeability without a follow-up /context call.
"""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from pytheum.api.annotators import (
    attach_cross_venue,
    attach_moves,
    attach_quote_staleness,
)
from pytheum.api.params import (
    book_from_payload,
    build_outcome_ladder,
    condition_id_from_payload,
    dedupe_markets_by_question,
    implied_yes_from_payload,
    market_event_key,
    parse_csv_list,
    parse_limit,
    resolution_from_payload,
    resolution_horizon,
    resolution_status_from_payload,
)

DEFAULT_LIMIT = 50
MAX_LIMIT = 200
_SCAN_RESOLUTION_CHARS = 240
_VALID_SORT = {"volume", "liquidity", "resolution", "move"}
# sort_by=move ranks by |move_24h| over a volume-bounded pool — liquid movers,
# not unranked noise (the benchmark's critique of SF's /api/changes). The pool
# bound is honest in meta.sorted_by.
_MOVE_POOL = 300
# How many children to surface in the bundle_outcomes ladder. The single
# favorite stays on bundle_top_outcome; this is the top-N for trading the whole
# bundle (#224 — the $64M "Fed Chair" bundle was unreadable from one favorite).
_BUNDLE_OUTCOMES_LIMIT = 5


def _num(v: str | None) -> float | None:
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _dt(v: str | None) -> datetime | None:
    """Parse an ISO date/datetime query param to a tz-aware datetime (asyncpg
    needs a datetime, not a str, for the timestamptz comparison). Unparseable
    -> None (filter ignored) rather than 500."""
    if not v:
        return None
    try:
        d = datetime.fromisoformat(v.replace("Z", "+00:00"))  # accepts date-only on 3.11+
        return d if d.tzinfo else d.replace(tzinfo=UTC)
    except ValueError:
        return None


async def _attach_bundle_top_outcome(markets: list[dict[str, Any]], *, dao: Any) -> None:
    """Mutate bundle-parent rows (implied_yes is None) to add BOTH the favorite
    child outcome (`bundle_top_outcome`) and the top-N priced-children ladder
    (`bundle_outcomes`), via one batched child query. The single favorite alone
    left mega-bundles like the $64M "Fed Chair" market unreadable (#224) — the
    ladder lets an agent read the whole field (Waller 60% / Warsh 30% / …) in the
    one /screen call. Fully guarded: no-op if the dao lacks batched children
    (minimal test daos) and never raises into the screen."""
    fetch = getattr(dao, "fetch_children_for_events", None)
    if fetch is None:
        return
    event_ids = [m["id"].split(":", 1)[1] for m in markets
                 if m.get("implied_yes") is None and m.get("venue") == "polymarket"
                 and ":" in (m.get("id") or "")]
    if not event_ids:
        return
    try:
        children = await fetch(event_ids)
    except Exception:  # enrichment must never break discovery
        return
    for m in markets:
        if m.get("implied_yes") is not None or ":" not in (m.get("id") or ""):
            continue
        kids = children.get(m["id"].split(":", 1)[1], [])
        if not kids:
            # No sibling legs → not a bundle/event parent, just an ordinary
            # binary row whose price isn't loaded. Leave it untouched (no
            # bundle fields) so binary rows aren't mislabeled as empty bundles.
            continue
        # build_outcome_ladder sorts priced children by implied_yes desc and
        # dedupes by outcome label — so ladder[0] IS the favorite. Deriving both
        # from one call keeps top_outcome and the ladder consistent by construction.
        ladder = build_outcome_ladder(kids, limit=_BUNDLE_OUTCOMES_LIMIT)
        if not ladder:
            # A real bundle parent (it HAS children) but none carry a price —
            # children all stale/eliminated/not-yet-refreshed (#229a: NBA MVP
            # polymarket:32754 surfaced bundle_top_outcome=null silently). Emit an
            # explicit reason so null reads as "known, no priced legs" not a bug.
            m["bundle_top_outcome"] = None
            m["bundle_top_reason"] = "no_priced_children"
            continue
        top = ladder[0]
        m["bundle_top_outcome"] = {
            "market_id": top.get("market_id"),
            "outcome": top.get("outcome"),
            "implied_yes": top.get("implied_yes"),
            "volume_usd": top.get("volume_usd"),
        }
        m["bundle_outcomes"] = ladder


async def handle_markets_screen(
    query: dict[str, str],
    *,
    dao: Any,
) -> tuple[int, dict[str, Any]]:
    limit = parse_limit(query, default=DEFAULT_LIMIT, max_limit=MAX_LIMIT)
    # Accept both `venues` (this endpoint's documented param) and `venue` (the
    # context/relevant-to param) — a trader probe filtered Kalshi with ?venue=
    # and got silently ignored (it fell back to the polymarket-dominated default).
    venues = parse_csv_list(query.get("venues") or query.get("venue")) or None
    status = query.get("status", "active") or None
    if status and status.lower() == "any":
        status = None
    sort_by = query.get("sort_by", "volume").lower()
    if sort_by not in _VALID_SORT:
        sort_by = "volume"
    exclude_stale = query.get("exclude_stale", "").lower() == "true"

    move_sort = sort_by == "move"
    rows = await dao.screen_markets(
        venues=venues,
        status=status,
        min_volume=_num(query.get("min_volume")),
        max_volume=_num(query.get("max_volume")),
        min_liquidity=_num(query.get("min_liquidity")),
        resolves_before=_dt(query.get("resolves_before")),
        resolves_after=_dt(query.get("resolves_after")),
        # Movers rank a top-volume pool client-side (the move column lives in
        # the price tables, not markets) — fetch the pool by volume first.
        sort_by="volume" if move_sort else sort_by,
        limit=_MOVE_POOL if move_sort else limit,
    )

    markets = []
    dropped_stale = 0
    for r in rows:
        days_to_resolution, is_stale = resolution_horizon(r.get("resolution_at"))
        # Drop only rows the VENUE no longer calls active (the settle-sweep,
        # scripts/sweep_settled_markets, keeps status truthful). A market whose
        # listed end date passed but that still trades on the venue — the
        # benchmark's Fujimori case, the week's biggest politics mover — stays
        # visible with is_stale=true as the informational flag.
        if exclude_stale and is_stale and (r.get("status") or "").lower() != "active":
            dropped_stale += 1
            continue
        markets.append({
            "id": r["id"],
            "question": r.get("question"),
            "venue": r.get("venue"),
            "bundle_id": r.get("bundle_id"),
            "bundle_label": r.get("bundle_label"),
            "status": r.get("status"),
            "volume_usd": r.get("volume_usd"),
            "liquidity_usd": r.get("liquidity_usd"),
            "url": r.get("url"),
            "resolution_at": (r["resolution_at"].isoformat()
                              if hasattr(r.get("resolution_at"), "isoformat") else r.get("resolution_at")),
            "days_to_resolution": days_to_resolution,
            "is_stale": is_stale,
            "implied_yes": implied_yes_from_payload(r.get("payload")),
            "book": book_from_payload(r.get("payload")),
            "resolution": (resolution_from_payload(r.get("payload")) or "")[
                :_SCAN_RESOLUTION_CHARS] or None,
            "resolution_status": resolution_status_from_payload(r.get("payload")),
            "condition_id": condition_id_from_payload(r.get("payload")),
            "event_key": market_event_key(r),
        })

    if move_sort:
        # Rank the volume pool by |move_24h| BEFORE the heavier per-row
        # attaches (staleness over 300 ids is the expensive one); keep 2x
        # limit slack for the dedupe below.
        await attach_moves(markets, dao=dao)
        markets.sort(key=lambda m: -abs(m.get("move_24h") or 0.0))
        markets = markets[:limit * 2]

    # #209: opaque bundle/event parents return implied_yes=null (World Cup, NBA
    # Champion, 2028 nominees — the highest-volume markets). Surface the favorite
    # child's price so they aren't blank in discovery. ONE batched child query
    # for the whole page (not N). Guarded so minimal test daos don't need it.
    await _attach_bundle_top_outcome(markets, dao=dao)
    # Quote-staleness inline (last_move_age_s + is_parked_wall) so an agent never
    # ranks a frozen parked-wall quote as a live edge without a follow-up call.
    await attach_quote_staleness(markets, dao=dao)
    # Verified cross-venue twin (#247) — {market_id, method, confidence} from the
    # matcher gold set, so the other venue's listing is one lookup away.
    await attach_cross_venue(markets, dao=dao)
    if not move_sort:
        # Movers inline on every screen row (#265/#214) — already attached
        # pre-sort in move mode.
        await attach_moves(markets, dao=dao)

    pre_dedup = len(markets)
    markets = dedupe_markets_by_question(markets)
    if move_sort:
        markets = markets[:limit]
    return 200, {
        "markets": markets,
        "count": len(markets),
        "meta": {
            "filters": {
                "venues": venues, "status": status, "sort_by": sort_by,
                "min_volume": _num(query.get("min_volume")),
                "max_volume": _num(query.get("max_volume")),
                "min_liquidity": _num(query.get("min_liquidity")),
                "resolves_before": query.get("resolves_before") or None,
                "resolves_after": query.get("resolves_after") or None,
                "exclude_stale": exclude_stale,
            },
            "limit": limit,
            "dropped_stale": dropped_stale,
            "deduped": pre_dedup - len(markets),
        },
    }
