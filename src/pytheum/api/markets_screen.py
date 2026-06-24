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

from pytheum.api._bounded_cache import BoundedTTLCache
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

# Short-TTL param-keyed response cache (mirrors markets_equivalents._cache).
# A load test showed /v1/markets/screen is the serving concurrency ceiling:
# p50 152ms -> 1056ms as concurrency goes 1->25 because every request runs a
# live Supabase query, while the already-cached /v1/markets/equivalents stays
# flat at ~70ms. Caching repeated/popular param-combos flattens the curve for
# exactly the bursts that saturate it. The markets table updates via
# ingest/price-sync, so 20s staleness is fine for a browse/discovery surface
# (per-quote staleness_seconds already signals freshness to clients). Only
# successful real (dao-backed) results are cached — never the dao=None
# degraded body.
_SCREEN_CACHE_TTL_S = 20.0
# Bounded so high-cardinality param-combos can't grow the cache unbounded
# (TTL purge on read + oldest-eviction on write). maxsize sized for the realistic
# distinct-param-combo working set on a browse surface.
_SCREEN_CACHE_MAXSIZE = 512
_screen_cache = BoundedTTLCache(ttl_s=_SCREEN_CACHE_TTL_S, maxsize=_SCREEN_CACHE_MAXSIZE)


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
    force_refresh: bool = False,
) -> tuple[int, dict[str, Any]]:
    # Never-500 convention: when booted without a DB (dao=None, secretless config)
    # return a structured 200 with degraded meta rather than crashing. NOT cached
    # — only successful real results below are.
    if dao is None:
        return 200, {
            "markets": [],
            "count": 0,
            "meta": {
                "degraded": True,
                "degraded_reason": "db_unavailable",
            },
        }
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
    min_volume = _num(query.get("min_volume"))
    max_volume = _num(query.get("max_volume"))
    min_liquidity = _num(query.get("min_liquidity"))
    resolves_before = _dt(query.get("resolves_before"))
    resolves_after = _dt(query.get("resolves_after"))

    # Param-keyed cache check (after parse so the key reflects NORMALIZED values:
    # the venue/venues alias collapses, sort_by/status fall back, dates parse to
    # the same datetime). Built from every param that reaches screen_markets +
    # the handler's post-fetch filters (exclude_stale), so two requests with
    # different params never collide and two identical requests hit. venues are
    # sorted so member-order doesn't fork the key.
    cache_key = (
        f"limit={limit}"
        f"|venues={','.join(sorted(venues)) if venues else ''}"
        f"|status={status}"
        f"|sort_by={sort_by}"
        f"|exclude_stale={exclude_stale}"
        f"|min_volume={min_volume}"
        f"|max_volume={max_volume}"
        f"|min_liquidity={min_liquidity}"
        f"|resolves_before={resolves_before.isoformat() if resolves_before else ''}"
        f"|resolves_after={resolves_after.isoformat() if resolves_after else ''}"
    )
    if not force_refresh:
        hit = _screen_cache.get(cache_key)
        if hit is not None:
            return 200, hit

    move_sort = sort_by == "move"
    rows = await dao.screen_markets(
        venues=venues,
        status=status,
        min_volume=min_volume,
        max_volume=max_volume,
        min_liquidity=min_liquidity,
        resolves_before=resolves_before,
        resolves_after=resolves_after,
        # Movers rank a top-volume pool client-side (the move column lives in
        # the price tables, not markets) — fetch the pool by volume first.
        sort_by="volume" if move_sort else sort_by,
        limit=_MOVE_POOL if move_sort else limit,
    )

    markets = []
    dropped_stale = 0
    for r in rows:
        days_to_resolution, is_stale = resolution_horizon(r.get("resolution_at"))
        # An EXPLICIT exclude_stale=true means a clean board: drop every is_stale
        # row (is_stale = past its resolution_at). The earlier grace-window guard
        # (a 2-day grace window past resolution) leaked decided markets
        # that the venue still flags status="active" (sweep lag) — a live trader
        # dogfood got 4 negative-DTE rows back despite exclude_stale (Spain v Cape
        # Verde -0.38d, Iran v NZ -0.0d, Topuria -0.87d). The is_stale flag is still
        # surfaced on every row for the DEFAULT view; only the explicit exclude drops.
        if exclude_stale and is_stale:
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
    body = {
        "markets": markets,
        "count": len(markets),
        "meta": {
            "filters": {
                "venues": venues, "status": status, "sort_by": sort_by,
                "min_volume": min_volume,
                "max_volume": max_volume,
                "min_liquidity": min_liquidity,
                "resolves_before": query.get("resolves_before") or None,
                "resolves_after": query.get("resolves_after") or None,
                "exclude_stale": exclude_stale,
            },
            "limit": limit,
            "dropped_stale": dropped_stale,
            "deduped": pre_dedup - len(markets),
        },
    }
    _screen_cache.set(cache_key, body)
    return 200, body
