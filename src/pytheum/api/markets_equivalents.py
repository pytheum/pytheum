"""Cross-venue equivalents API handlers.

Two endpoints:

GET /v1/markets/equivalents
    Collection-level: returns the full live-filtered equivalence set joined to
    live quotes. t_find_divergences consumes this for the verified-pair
    divergence scan (#247). Pairs are pre-decided by the cross-venue matcher
    (136,877 pairs in the 2026-06-12 export); we don't match here.

GET /v1/markets/{ref}/equivalents
    Per-ref: given a market ref on either venue, return its settlement-verified
    counterpart(s) from the equivalence dataset, with live price data hydrated
    from the market store where available.
"""
from __future__ import annotations

import asyncio
from typing import Any

from pytheum.api._bounded_cache import BoundedTTLCache
from pytheum.api.annotators import attach_quote_staleness
from pytheum.api.params import (
    book_from_payload,
    implied_yes_from_payload,
    parse_limit,
    resolution_from_payload,
    resolution_horizon,
)
from pytheum.api.ref_utils import normalize_ref
from pytheum.equivalence.index import is_fungible_method
from pytheum.equivalence.orientation import orient_pair, outcomes_from_payload

# ---------------------------------------------------------------------------
# Collection endpoint constants + cache
# ---------------------------------------------------------------------------

DEFAULT_LIMIT = 50
MAX_LIMIT = 200
# Over-fetch tuning for the collection browse.  The export is liveness-ordered
# (soonest-resolving first), so already-resolved pairs cluster at the FRONT —
# a naive first-`limit` slice returns an all-dead page once the export ages a
# day (every soonest-first row has since resolved and gets swept stale).  So:
# (1) skip row-level-stale rows up front via the export's resolution_date (cheap,
#     no DB) while scanning at most _SCAN_BUDGET_ROWS rows, and
# (2) hydrate a multiple of the page (_OVERFETCH_FACTOR, capped at
#     _MAX_CANDIDATES) so the handler's authoritative book-level stale drops
#     don't starve the result below `limit`.
_SCAN_BUDGET_ROWS = 3000
_OVERFETCH_FACTOR = 4
# Raised from 300: the page now also skips one-sided pairs (a leg without a
# two-sided book — see _has_two_sided_book), so when a near-term cluster of
# one-sided pairs dominates the soonest-resolving front we must hydrate deeper
# to reach `limit` genuinely-comparable both-priced pairs. Hydration stays 2
# batched queries (fetch_markets_by_ids + attach_quote_staleness) over the
# candidate id set — the book-presence check itself is in-memory, no extra fetch.
_MAX_CANDIDATES = 500
# Floor on the hydration set regardless of `limit`.  A small `limit` would
# otherwise hydrate only the un-ingested/one-sided soonest front and surface few
# complete pairs — the gold set is liveness-ordered and its recent front is
# heavily un-hydratable (PM legs not yet in the markets table), so a shallow
# scan starves the page (measured: default limit=50 returned count=1 at floor
# 150 while the limit=150 scanner returned 28).  Raised 150 → 500 (= _MAX_CANDIDATES)
# on 2026-06-24 once the box was rightsized t3.large → m6i.xlarge (4 vCPU / 16 GB,
# swap-thrash gone): the default page now hydrates to scanner depth so it reaches
# the live both-priced pairs past the front.  Hydration is still 2 batched queries
# over the candidate id set; affordable on the bigger box's warm-loop 60s refresh.
# (Root cure for the un-hydratable front is ingest coverage — ali/matcher; this
# is the serving-side mitigation.)
_MIN_CANDIDATES = 500

# The pairs join (136k equivalence rows x markets) plus the 300-id staleness
# window run ~20s on the shared-compute DB — past the MCP transport's patience
# (the 2026-06-11 benchmark saw every t_find_divergences call die "Server
# disconnected" once 6 concurrent agents kept the cache cold).  Everything
# read here is a slow-moving snapshot, so the server WARMS the cache itself
# (warm_equivalents_loop, every _WARM_INTERVAL_S) and callers always hit it;
# the TTL only matters if the warmer dies.
_CACHE_TTL_S = 180.0
_WARM_INTERVAL_S = 60.0
_WARM_KEYS = ("150", "50")  # t_find_divergences fetch + endpoint default
# Bounded: keys are low-cardinality (limit:fungible:rules:status combos) but the
# bodies can be large, so a hard cap (+ TTL purge on read / oldest-eviction on
# write) keeps memory bounded regardless of key churn.
_CACHE_MAXSIZE = 256
_cache = BoundedTTLCache(ttl_s=_CACHE_TTL_S, maxsize=_CACHE_MAXSIZE)


# ---------------------------------------------------------------------------
# Collection endpoint helpers
# ---------------------------------------------------------------------------

def _leg(r: dict[str, Any], *, include_rules: bool = False) -> dict[str, Any]:
    """Same lean shape as a /screen row — enough for fee-netting + edge math
    + staleness gating without a follow-up call.

    When ``include_rules=True`` the ``resolution`` field is added (full
    settlement rules text from the market's payload, truncated to
    ``_MAX_RESOLUTION_CHARS`` by ``resolution_from_payload``).  Omitted by
    default to keep the collection-endpoint payload size small; the MCP
    divergence scanner opts in via ``include_rules=true``.
    """
    days_to_resolution, is_stale = resolution_horizon(r.get("resolution_at"))
    leg: dict[str, Any] = {
        "id": r["id"],
        "question": r.get("question"),
        "venue": r.get("venue"),
        "status": r.get("status"),
        "volume_usd": r.get("volume_usd"),
        "liquidity_usd": r.get("liquidity_usd"),
        "url": r.get("url"),
        "resolution_at": (r["resolution_at"].isoformat()
                          if hasattr(r.get("resolution_at"), "isoformat")
                          else r.get("resolution_at")),
        "days_to_resolution": days_to_resolution,
        "is_stale": is_stale,
        "implied_yes": implied_yes_from_payload(r.get("payload")),
        "book": book_from_payload(r.get("payload")),
    }
    if include_rules:
        leg["resolution"] = resolution_from_payload(r.get("payload"))
    return leg


def _has_two_sided_book(leg: dict[str, Any]) -> bool:
    """True when the leg carries a two-sided book (both bid and ask present).

    A cross-venue spread/edge is only computable when BOTH legs are two-sided;
    a one-sided or absent book (venue not quoting — e.g. a closed/illiquid leg
    of a matched pair) can't show an edge.  The collection browse skips such
    pairs so a near-term cluster of one-sided pairs at the soonest-resolving
    front doesn't crowd out genuinely-comparable both-priced pairs.  In-memory
    on the already-hydrated leg — no extra fetch.
    """
    book = leg.get("book")
    return bool(book and book.get("bid") is not None and book.get("ask") is not None)


def _index_rows_to_pairs(
    rows: list[dict[str, Any]], *, limit: int, fungible_only: bool = False,
    scan_budget: int = _SCAN_BUDGET_ROWS, skip_row_stale: bool = True,
) -> list[dict[str, Any]]:
    """Translate EquivalenceIndex export rows to the DB pair format used by
    the collection handler.  poly_side / poly_outcome are null because the
    side-map is not included in the file export.

    When ``fungible_only=True`` rows whose ``method`` is not deterministic /
    human-adjudicated (see ``is_fungible_method``) are skipped.

    The export is soonest-resolving-first, so already-resolved pairs cluster at
    the front.  When ``skip_row_stale`` (default), rows whose ``resolution_date``
    is already in the past are skipped here — cheap, no DB — so they don't crowd
    out live pairs before the handler's authoritative book-level staleness check.
    At most ``scan_budget`` rows are examined so an aged/large dead front can't
    turn this into a full-corpus scan on every request.
    """
    pairs: list[dict[str, Any]] = []
    for examined, row in enumerate(rows):
        if examined >= scan_budget:
            break
        if fungible_only and not is_fungible_method(row.get("method")):
            continue
        k_ref = row.get("kalshi_ref")
        p_ref = row.get("pm_ref")
        if not k_ref or not p_ref:
            continue
        if skip_row_stale:
            _, row_stale = resolution_horizon(row.get("resolution_date"))
            if row_stale:
                continue
        pairs.append({
            "kalshi_market_id": k_ref,
            "polymarket_market_id": p_ref,
            "method": row.get("method"),
            "confidence": row.get("confidence"),
            "bet_type": row.get("bet_type"),
            "poly_side": None,
            "poly_outcome": None,
        })
        if len(pairs) >= limit:
            break
    return pairs


def _parse_bool_param(query: dict[str, str], key: str, *, default: bool) -> bool:
    """Parse a boolean query param; accepts 'true'/'1'/'yes' as truthy."""
    raw = (query.get(key) or "").strip().lower()
    if not raw:
        return default
    return raw in ("true", "1", "yes")


_VALID_STATUSES = ("live", "settled", "all")


def _parse_status_param(query: dict[str, str]) -> str | None:
    """Parse the ``status`` query param (case-insensitive, trimmed).

    Returns the normalised value (``live`` | ``settled`` | ``all``), defaulting
    to ``live`` when empty/missing.  Returns ``None`` on an unknown value so the
    handler can surface a 400.
    """
    raw = (query.get("status") or "").strip().lower()
    if not raw:
        return "live"
    if raw not in _VALID_STATUSES:
        return None
    return raw


async def handle_markets_equivalents(
    query: dict[str, str],
    *,
    dao: Any,
    equivalence: Any = None,
    force_refresh: bool = False,
) -> tuple[int, dict[str, Any]]:
    """GET /v1/markets/equivalents — collection of verified cross-venue pairs.

    ``equivalence`` accepts an EquivalenceIndex (136,877 pairs, loaded from
    the matcher export file).  When provided, pairs are sourced from the index
    (faster, avoids a DB join).  When omitted the handler falls back to
    ``dao.fetch_equivalence_pairs()`` — the original DB-backed path still used
    by tests and any caller that doesn't have the index mounted.

    Query parameters
    ----------------
    fungible_only : bool (default False)
        When true, restrict to pairs whose method indicates deterministic
        structural or human-adjudicated equivalence (see ``is_fungible_method``).
        LLM-judged pairs (opus_backstop, llm_local) are excluded.
    include_rules : bool (default False)
        When true, each leg carries a ``resolution`` field (full settlement
        rules text from the market's payload).  Omitted by default to keep
        collection-endpoint payload size small; the MCP divergence scanner
        opts in to pull rules for each pair.
    status : str (default "live")
        Resolution filter for returned pairs.
          - ``live``    (default) — only tradeable pairs: skip row-stale rows up
            front, drop pairs whose either hydrated leg is settled (is_stale),
            and drop one-sided pairs (a leg without a two-sided book).
          - ``settled`` — only settled pairs (EITHER hydrated leg is_stale).
            Does NOT skip row-stale rows up front and does NOT drop one-sided
            pairs (settled legs are commonly one-sided / unquoted); live pairs
            are dropped instead (counted in ``meta.dropped_live``).
          - ``all`` — no resolution filter and no one-sided drop; every hydrated
            pair (both legs present) up to ``limit``.
    """
    limit = parse_limit(query, default=DEFAULT_LIMIT, max_limit=MAX_LIMIT)
    fungible_only = _parse_bool_param(query, "fungible_only", default=False)
    include_rules = _parse_bool_param(query, "include_rules", default=False)
    status = _parse_status_param(query)
    if status is None:
        return 400, {
            "error": "invalid status; must be one of: "
                     + ", ".join(_VALID_STATUSES)
        }
    cache_key = f"{limit}:{fungible_only}:{include_rules}:{status}"
    if not force_refresh:
        hit = _cache.get(cache_key)
        if hit is not None:
            return 200, hit

    # Lazy-load the equivalence singleton when not injected (same pattern as
    # handle_markets_matched / handle_market_rules / handle_status).  The
    # singleton is already pre-warmed at boot so this is a fast dict lookup.
    if equivalence is None:
        from pytheum.equivalence.index import get_index
        equivalence = get_index()

    # Source pairs from index (preferred) or DAO fallback.
    # When dao=None (secretless / no-DB config) the DAO path is skipped; the
    # index path still provides the full pair set, just without live hydration.
    # Over-fetch CANDIDATES (not just `limit`): the soonest-first export ages so
    # its front resolves day-over-day, and book-level staleness drops more — so
    # we scan/hydrate a multiple of the page and truncate to `limit` LIVE pairs
    # below.  Without this, an aged export returns an empty page (the bug).
    candidate_cap = min(max(limit * _OVERFETCH_FACTOR, _MIN_CANDIDATES), _MAX_CANDIDATES)
    # For settled/all the row-stale skip is OFF: settled pairs are exactly the
    # row-stale front the live path would otherwise skip, so dropping them up
    # front would starve those modes.  The soonest-first export clusters settled
    # pairs at the front, so settled still finds them early within the budget.
    skip_row_stale = status == "live"
    if equivalence is not None:
        pairs = _index_rows_to_pairs(equivalence._rows, limit=candidate_cap,
                                     fungible_only=fungible_only,
                                     skip_row_stale=skip_row_stale)
    elif dao is not None:
        pairs = await dao.fetch_equivalence_pairs(limit=candidate_cap)
    else:
        pairs = []

    legs: dict[str, dict[str, Any]] = {}
    pm_outcomes: dict[str, list[str] | None] = {}
    if pairs and dao is not None:
        ids = sorted({p["kalshi_market_id"] for p in pairs}
                     | {p["polymarket_market_id"] for p in pairs})
        for r in await dao.fetch_markets_by_ids(ids):
            legs[r["id"]] = _leg(r, include_rules=include_rules)
            # Keep the PM outcome names aside (not in the leg response — kept small) for
            # orient-at-serve below.
            pm_outcomes[r["id"]] = outcomes_from_payload(r.get("payload"))
        # Staleness inline so a parked-wall leg never reads as a live edge.
        await attach_quote_staleness(list(legs.values()), dao=dao)

    out = []
    dropped_stale = 0
    dropped_one_sided = 0
    dropped_live = 0
    for p in pairs:
        a = legs.get(p["kalshi_market_id"])
        b = legs.get(p["polymarket_market_id"])
        if a is None or b is None:
            continue
        # A pair is "settled" when EITHER hydrated leg's resolution has passed
        # (is_stale) — venues carry resolved game markets as status=active
        # (#213), and those ghosts (one side parked at 0.999/0.0005) would
        # otherwise dominate any live edge ranking.
        pair_settled = bool(a.get("is_stale") or b.get("is_stale"))
        if status == "live" and pair_settled:
            dropped_stale += 1
            continue
        # Invert the live filter for settled: keep ONLY settled pairs, drop live.
        if status == "settled" and not pair_settled:
            dropped_live += 1
            continue
        # status == "all": no resolution filter.
        # One-sided drop applies ONLY to the live path — a leg without a
        # two-sided book can't show a cross-venue spread, and a near-term
        # cluster of these would crowd out genuinely-comparable both-priced
        # pairs.  settled/all keep one-sided pairs (settled legs are commonly
        # unquoted; `all` is an unfiltered dump).  Cheap in-memory check; the
        # over-fetch (above) hydrates deep enough to scan past them.
        if status == "live" and not (_has_two_sided_book(a) and _has_two_sided_book(b)):
            dropped_one_sided += 1
            continue
        # Orient-at-serve: if the matcher/side-map didn't already set poly_side, derive it
        # inline from the Kalshi title ('Will X win' → YES team) + the PM outcomes, for the
        # team/total bet types. This orients the perishable daily front (which resolves before
        # a pre-computed pair_side_map run can catch it) without a stored table — fresh every
        # request. Conservative (orient_pair returns None on any ambiguity), and ADDITIVE: it
        # only fills a null poly_side, never overrides an existing one, so it can't invert.
        poly_side = p.get("poly_side")
        poly_outcome = p.get("poly_outcome")
        if poly_side is None:
            poly_side, poly_outcome = orient_pair(
                p.get("bet_type"), a.get("question"), pm_outcomes.get(p["polymarket_market_id"]))
        out.append({
            "method": p.get("method"),
            "confidence": p.get("confidence"),
            "bet_type": p.get("bet_type"),
            # Which poly outcome index the Kalshi YES side maps to (matcher side-map OR
            # orient-at-serve); null = couldn't orient unambiguously → scanner won't edge-score.
            "poly_side": poly_side,
            "poly_outcome": poly_outcome,
            "a": a,
            "b": b,
        })
        # We over-fetched candidates; stop once the page is full of LIVE pairs.
        if len(out) >= limit:
            break
    body = {
        "pairs": out,
        "count": len(out),
        "meta": {
            "limit": limit,
            "status": status,
            "dropped_stale": dropped_stale,
            "dropped_one_sided": dropped_one_sided,
            "dropped_live": dropped_live,
            "candidates_hydrated": len(pairs),
            "fungible_only": fungible_only,
            "include_rules": include_rules,
            "cache_ttl_s": _CACHE_TTL_S,
            "source": "pytheum-cross-venue-matcher gold set (pre-decided pairs)",
        },
    }
    _cache.set(cache_key, body)
    return 200, body


async def warm_equivalents_loop(*, dao: Any, stop: Any) -> None:
    """Keep the equivalents cache permanently warm so no caller ever pays the
    ~20s cold path (which exceeds the MCP transport's patience under load).
    Runs in the server process; failures log and retry next cycle."""
    import logging

    logger = logging.getLogger(__name__)
    while not stop.is_set():
        for key in _WARM_KEYS:
            try:
                await handle_markets_equivalents({"limit": key}, dao=dao,
                                                 force_refresh=True)
            except Exception:
                logger.exception("equivalents cache warm failed (limit=%s)", key)
        try:
            await asyncio.wait_for(stop.wait(), timeout=_WARM_INTERVAL_S)
        except TimeoutError:
            continue


# ---------------------------------------------------------------------------
# Per-ref endpoint helpers
# ---------------------------------------------------------------------------

# Venue names that each matched_via key belongs to
_MATCHED_VIA_VENUE: dict[str, str] = {
    "kalshi_ticker": "kalshi",
    "pm_gamma_id": "polymarket",
    "pm_condition_id": "polymarket",
    "pm_slug": "polymarket",
}


def _row_to_market_block(row: dict[str, Any], ref: str) -> dict[str, Any]:
    """Build a hydrated market block from a DAO row."""
    payload = row.get("payload")
    days, is_stale = resolution_horizon(row.get("resolution_at"))
    return {
        "id": row.get("id", ref),
        "question": row.get("question"),
        "venue": row.get("venue"),
        "status": row.get("status"),
        "volume_usd": row.get("volume_usd"),
        "liquidity_usd": row.get("liquidity_usd"),
        "url": row.get("url"),
        "resolution_at": (
            row["resolution_at"].isoformat()
            if hasattr(row.get("resolution_at"), "isoformat")
            else row.get("resolution_at")
        ),
        "days_to_resolution": days,
        "is_stale": is_stale,
        "implied_yes": implied_yes_from_payload(payload),
        "book": book_from_payload(payload),
    }


def _minimal_market_block(ref: str, *, question: str | None, venue: str | None) -> dict[str, Any]:
    """Minimal market block when the market isn't in the platform store."""
    return {"id": ref, "question": question, "venue": venue}


def _build_equivalent_item(
    pair: dict[str, Any],
    *,
    counterpart_ref: str,
    counterpart_venue: str,
    counterpart_row: dict[str, Any] | None,
) -> dict[str, Any]:
    """Build one item in the equivalents list."""
    export_question = (
        pair.get("kalshi_title") if counterpart_venue == "kalshi" else pair.get("pm_title")
    )
    if counterpart_row is None:
        return {
            "id": counterpart_ref,
            "venue": counterpart_venue,
            "question": export_question,
            "bet_type": pair.get("bet_type"),
            "poly_side": pair.get("poly_side"),
            "confidence": pair.get("confidence"),
            "method": pair.get("method"),
            "implied_yes": None,
            "book": None,
            "volume_usd": None,
            "url": None,
        }
    payload = counterpart_row.get("payload")
    return {
        "id": counterpart_ref,
        "venue": counterpart_venue,
        "question": counterpart_row.get("question") or export_question,
        "bet_type": pair.get("bet_type"),
        "poly_side": pair.get("poly_side"),
        "confidence": pair.get("confidence"),
        "method": pair.get("method"),
        "implied_yes": implied_yes_from_payload(payload),
        "book": book_from_payload(payload),
        "volume_usd": counterpart_row.get("volume_usd"),
        "url": counterpart_row.get("url"),
    }


async def _hydrate(ref: str, dao: Any) -> dict[str, Any] | None:
    """Try dao.fetch_market; return None on any failure."""
    try:
        return await dao.fetch_market(ref)  # type: ignore[no-any-return]
    except Exception:
        return None


async def handle_market_equivalents(
    ref: str,
    query: dict[str, str],
    *,
    dao: Any,
    equivalence: Any = None,
) -> tuple[int, dict[str, Any]]:
    """GET /v1/markets/{ref}/equivalents handler.

    ``equivalence`` accepts an EquivalenceIndex (or duck-typed equivalent with
    .lookup() / .pairs_loaded / .dataset_version / .file_missing / .load_error).
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

    # If lookup was via condition_id or slug, try the canonical numeric pm_ref as fallback
    if focal_row is None and pairs and matched_via in ("pm_condition_id", "pm_slug"):
        canonical = pairs[0].get("pm_ref")
        if canonical and canonical != ref_norm:
            focal_row = await _hydrate(canonical, dao)

    # If raw (no-prefix) kalshi ticker, try with prefix
    if focal_row is None and pairs and matched_via == "kalshi_ticker" and ":" not in ref_norm:
        focal_row = await _hydrate(f"kalshi:{ref_norm}", dao)

    # Focal market block
    focal_question: str | None = None
    if pairs:
        focal_question = (
            pairs[0].get("kalshi_title") if focal_venue == "kalshi"
            else pairs[0].get("pm_title")
        )

    market_block = (
        _row_to_market_block(focal_row, ref_norm)
        if focal_row is not None
        else _minimal_market_block(ref_norm, question=focal_question, venue=focal_venue)
    )

    # Determine counterpart venue
    if focal_venue == "kalshi":
        counterpart_venue = "polymarket"
        counterpart_ref_key = "pm_ref"
    elif focal_venue == "polymarket":
        counterpart_venue = "kalshi"
        counterpart_ref_key = "kalshi_ref"
    else:
        counterpart_venue = ""
        counterpart_ref_key = ""

    # Build equivalents list
    equivalents: list[dict[str, Any]] = []
    for pair in pairs:
        counterpart_ref = pair.get(counterpart_ref_key, "") if counterpart_ref_key else ""
        if not counterpart_ref:
            continue
        counterpart_row = await _hydrate(counterpart_ref, dao)
        equivalents.append(
            _build_equivalent_item(
                pair,
                counterpart_ref=counterpart_ref,
                counterpart_venue=counterpart_venue,
                counterpart_row=counterpart_row,
            )
        )

    # Cross-venue spread block
    cross_venue: dict[str, Any] = {}
    focal_implied = market_block.get("implied_yes") if focal_row is not None else None
    kalshi_implied: float | None = None
    pm_implied: float | None = None

    # Orientation signals from the matched pair (mirrors t_find_divergences): the
    # PM implied_yes tracks its FIRST-LISTED token, which may be the OPPOSITE side
    # of the Kalshi YES on a moneyline. poly_side==1 => flip; an "event" pair maps
    # market-to-market and needs no flip.
    _poly_side: Any = None
    _bet_type: Any = None
    if focal_venue == "kalshi":
        kalshi_implied = focal_implied
        for eq in equivalents:
            if eq.get("venue") == "polymarket" and eq.get("implied_yes") is not None:
                pm_implied = eq["implied_yes"]
                _poly_side, _bet_type = eq.get("poly_side"), eq.get("bet_type")
                break
    elif focal_venue == "polymarket":
        pm_implied = focal_implied
        for eq in equivalents:
            if eq.get("venue") == "kalshi" and eq.get("implied_yes") is not None:
                kalshi_implied = eq["implied_yes"]
                _poly_side, _bet_type = eq.get("poly_side"), eq.get("bet_type")
                break

    # Re-orient the PM side into the Kalshi-YES frame when the verified side-map
    # says so (side==1 flips; side==0 / event = no flip).
    if pm_implied is not None and _poly_side == 1:
        pm_implied = round(1.0 - pm_implied, 6)

    if kalshi_implied is not None:
        cross_venue["kalshi_implied"] = kalshi_implied
    if pm_implied is not None:
        cross_venue["pm_implied"] = pm_implied
    if kalshi_implied is not None and pm_implied is not None:
        # Only emit a spread when orientation is KNOWN. A non-event pair with no
        # verified side-map may be comparing OPPOSITE sides → a blind kalshi−pm
        # subtraction is ~2× wrong (probe 2026-06-15: UFC Pereira-vs-Gane printed
        # 0.065 vs true ~0.035). Mirror t_find_divergences: never guess.
        if _bet_type == "event" or _poly_side is not None:
            cross_venue["spread"] = round(kalshi_implied - pm_implied, 4)
        else:
            cross_venue["spread"] = None
            cross_venue["spread_unavailable"] = (
                "unoriented: pair lacks a verified side-map; use t_find_divergences "
                "for oriented cross-venue edges")

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
        "market": market_block,
        "equivalents": equivalents,
        "cross_venue": cross_venue,
        "meta": meta,
    }
