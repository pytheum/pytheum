"""GET /v1/markets/search — text search over market titles (t_search_markets).

The DISCOVERY counterpart to /screen and /relevant-to: a keyless, NON-semantic
substring/token search over market titles on both venues. Where /relevant-to is
the semantic (embedding-backed) "find markets like this article" path, /search
is the cheap, exact "find markets whose title contains these words" path — no
Pinecone / OpenAI round-trip, so it stays free on the hosted tier and never
misses an exact term (H5N1, a player name, a ticker) the way kNN can.

Tokens are AND-matched against the title (DAO.search_markets_by_title), ranked by
volume_usd desc. Rows use the same fat shape as /screen (book / resolution /
condition_id / cross-venue twin / quote-staleness) so an agent can triage
tradeability without a follow-up /core call.
"""
from __future__ import annotations

from typing import Any

from pytheum.api.annotators import (
    attach_cross_venue,
    attach_moves,
    attach_quote_staleness,
)
from pytheum.api.params import (
    book_from_payload,
    condition_id_from_payload,
    dedupe_markets_by_question,
    implied_yes_from_payload,
    market_event_key,
    parse_csv_list,
    parse_limit,
    parse_payload,
    resolution_from_payload,
    resolution_horizon,
    resolution_status_from_payload,
)

DEFAULT_LIMIT = 50
MAX_LIMIT = 200
_SCAN_RESOLUTION_CHARS = 240
# The DAO AND-matches at most 4 tokens against the title; tokenizing more than
# that is wasted work (and over-narrows). Mirror the DAO's cap here so the meta
# block honestly reports which tokens actually filtered.
_MAX_TOKENS = 4


def _tokenize(q: str) -> list[str]:
    """Split a free-text query into AND-match tokens. Whitespace-split, drop
    empties, cap at _MAX_TOKENS. Tokens are matched case-insensitively (ILIKE) by
    the DAO, so we keep original case here and let the DB fold it."""
    return [t for t in q.split() if t][:_MAX_TOKENS]


async def handle_markets_search(
    query: dict[str, str],
    *,
    dao: Any,
) -> tuple[int, dict[str, Any]]:
    """GET /v1/markets/search handler.

    Query params:
      q        — required free-text search string (AND-matched title tokens).
      venues   — comma-separated venue filter (kalshi, polymarket, manifold).
      status   — market status filter (default 'active'; 'any'/'all' → no filter).
      limit    — max results (default 50, max 200).

    Never-500 convention: a missing `q` returns a structured 400-shaped 200 body
    with an empty list + error, and a None dao (secretless boot) returns a
    degraded 200 rather than crashing — same contract as /screen.
    """
    raw_q = (query.get("q") or query.get("query") or "").strip()
    tokens = _tokenize(raw_q)
    if not tokens:
        return 200, {
            "markets": [],
            "count": 0,
            "meta": {
                "error": "missing_query",
                "hint": "q is required — a non-empty search string, e.g. ?q=super+bowl.",
            },
        }
    if dao is None:
        return 200, {
            "markets": [],
            "count": 0,
            "meta": {"degraded": True, "degraded_reason": "db_unavailable", "query": raw_q},
        }

    limit = parse_limit(query, default=DEFAULT_LIMIT, max_limit=MAX_LIMIT)
    # Accept both `venues` (screen's documented param) and `venue` (the
    # context/relevant-to param) — same dual-accept as /screen so a trader who
    # filtered with ?venue= isn't silently given the unfiltered universe.
    venues = parse_csv_list(query.get("venues") or query.get("venue")) or None
    status: str | None = (query.get("status") or "active").strip() or "active"
    if status and status.lower() in ("any", "all"):
        status = None
    statuses = [status] if status else None

    search = getattr(dao, "search_markets_by_title", None)
    if search is None:
        # A minimal/older dao without the search method: degrade rather than 500.
        return 200, {
            "markets": [],
            "count": 0,
            "meta": {"degraded": True, "degraded_reason": "search_unavailable", "query": raw_q},
        }

    rows = await search(tokens, venues=venues, statuses=statuses, limit=limit)

    markets: list[dict[str, Any]] = []
    for r in rows:
        # search_markets_by_title keys the id as `market_id` (knn_markets shape);
        # normalize to `id` so the row matches /screen's contract + the annotators.
        mid = r.get("market_id") or r.get("id")
        days_to_resolution, is_stale = resolution_horizon(r.get("resolution_at"))
        # Parse the payload ONCE (it's a JSON string from the DB) and feed the dict to every
        # *_from_payload helper — they each re-parse a string but pass a dict straight through. On
        # a common-term page (50 rows) this is 50 parses instead of ~250 (the residual after the
        # late-materialization fix).
        pl = parse_payload(r.get("payload"))
        markets.append({
            "id": mid,
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
            "implied_yes": implied_yes_from_payload(pl),
            "book": book_from_payload(pl),
            "resolution": (resolution_from_payload(pl) or "")[
                :_SCAN_RESOLUTION_CHARS] or None,
            "resolution_status": resolution_status_from_payload(pl),
            "condition_id": condition_id_from_payload(pl),
            "event_key": market_event_key({**r, "market_id": mid}),
        })

    # Same enrichment chain as /screen so search rows are triage-ready: quote
    # staleness (don't rank a parked wall), verified cross-venue twin, and moves.
    await attach_quote_staleness(markets, dao=dao)
    await attach_cross_venue(markets, dao=dao)
    await attach_moves(markets, dao=dao)

    pre_dedup = len(markets)
    markets = dedupe_markets_by_question(markets)

    return 200, {
        "markets": markets,
        "count": len(markets),
        "meta": {
            "query": raw_q,
            "tokens": tokens,
            "filters": {"venues": venues, "status": status},
            "limit": limit,
            "deduped": pre_dedup - len(markets),
        },
    }
