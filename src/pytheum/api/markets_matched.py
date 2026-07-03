"""GET /v1/markets/matched — browse cross-venue matched pairs.

Returns a paginated, optionally filtered view of the full pytheum equivalence
dataset (136k settlement-verified Kalshi<->Polymarket pairs), with live prices
hydrated from the market store where available.

Query parameters
----------------
bet_type : str, optional
    Comma-separated bet_type names **or** the group alias ``sports``.
    Group names are expanded via ``EquivalenceIndex.BET_TYPE_GROUPS``.
q : str, optional
    Case-insensitive substring filter over both sides' titles.
min_volume : float, optional
    Minimum volume_usd on the *hydrated* focal side (Kalshi leg).  Pairs where
    the focal side is not in the store are included (filter only skips when we
    have a volume and it is below the threshold).
sort_by : str, default "volume"
    Sort order for returned pairs.  Accepted values:

    * ``volume``  — hydrated Kalshi volume desc, then confidence desc (default).
    * ``net_edge`` — executable, fee-netted locked-arb edge desc (the HONEST arb
                     radar); pairs without a two-sided book on both legs sort last.
    * ``spread``  — |kalshi_implied − pm_implied| desc; pairs where either
                    implied price is missing sort last. NOTE: mid-based, overstates
                    one-sided books — prefer ``net_edge`` for real arb ranking.
    * ``confidence`` — match confidence desc.

    Unknown values silently fall back to ``"volume"``.

    Each pair's ``cross_venue`` block carries ``net_edge`` (executable, fee-netted)
    and ``executable`` (bool) alongside the mid ``spread`` when both legs are booked.
limit : int, default 50, max 200
offset : int, default 0
"""
from __future__ import annotations

import contextlib
from typing import Any

from pytheum.api._bounded_cache import BoundedTTLCache
from pytheum.api.params import (
    book_from_payload,
    implied_yes_from_payload,
    locked_arb_net_edge,
    parse_limit,
)

# Short-TTL param-keyed response cache (mirrors markets_screen._screen_cache).
# A load test showed /v1/markets/matched is a concurrency cliff: p50
# 156ms -> 789ms -> 1978ms (p95 3710ms) as concurrency goes 1->10->25 because
# every request runs a live fetch_markets_by_ids over the page's leg ids (the
# same uncached-DB pattern /screen and /equivalents had). Matched pairs are a
# slow-moving, settlement-verified cross-venue collection hydrated with live
# quotes, so 20s staleness is fine for this browse surface and caching repeated
# param-combos flattens the curve for exactly the bursts that saturate it. Only
# successful real (non-degraded) results are cached — never the degraded
# equivalence-unavailable body.
_MATCHED_CACHE_TTL_S = 20.0
# Bounded so high-cardinality param-combos can't grow the cache unbounded
# (TTL purge on read + oldest-eviction on write).
_MATCHED_CACHE_MAXSIZE = 512
_matched_cache = BoundedTTLCache(ttl_s=_MATCHED_CACHE_TTL_S, maxsize=_MATCHED_CACHE_MAXSIZE)


def _parse_offset(query: dict[str, str]) -> int:
    try:
        return max(0, int(query.get("offset", 0)))
    except (ValueError, TypeError):
        return 0


_VALID_SORT_BY = frozenset({"volume", "spread", "confidence", "net_edge"})


def _parse_sort_by(query: dict[str, str]) -> str:
    """Parse sort_by param; silently falls back to 'volume' for unknown values."""
    raw = (query.get("sort_by") or "").strip().lower()
    return raw if raw in _VALID_SORT_BY else "volume"


def _parse_min_volume(query: dict[str, str]) -> float | None:
    raw = query.get("min_volume")
    if raw is None:
        return None
    try:
        return float(raw)
    except (ValueError, TypeError):
        return None


def _parse_league(query: dict[str, str]) -> str | None:
    """Return the league filter value, stripped; None when not provided."""
    raw = query.get("league")
    if not raw:
        return None
    stripped = raw.strip()
    return stripped or None


_DATE_RE = __import__("re").compile(r"^\d{4}-\d{2}-\d{2}$")


def _parse_game_date(query: dict[str, str]) -> str | None:
    """Return the game_date filter (YYYY-MM-DD), or None when absent/malformed."""
    raw = query.get("date") or query.get("game_date")
    if not raw:
        return None
    stripped = raw.strip()
    if _DATE_RE.match(stripped):
        return stripped
    return None  # silently ignore malformed dates


def _parse_fungible_only(query: dict[str, str]) -> bool:
    """Return True when the request opts into fungible-only pairs.

    Accepted truthy values (case-insensitive): "true", "1", "yes".
    Everything else (including absent) → False.
    """
    raw = (query.get("fungible_only") or "").strip().lower()
    return raw in ("true", "1", "yes")


def _parse_bet_type_filter(
    raw: str | None,
    *,
    groups: dict[str, set[str]],
    available: set[str],
) -> set[str] | None:
    """Expand a CSV bet_type param (possibly containing group names) into a set
    of concrete bet_type values.  Returns None when no filter was requested."""
    if not raw:
        return None
    result: set[str] = set()
    for tok in raw.split(","):
        tok = tok.strip()
        if not tok:
            continue
        if tok in groups:
            result |= groups[tok]
        else:
            result.add(tok)
    return result or None


def _hydrate_side(
    ref: str | None,
    title: str | None,
    venue: str | None,
    row: dict[str, Any] | None,
) -> dict[str, Any]:
    """Build one side of the pair block (kalshi or polymarket)."""
    if row is None:
        return {
            "id": ref,
            "question": title,
            "venue": venue,
            "implied_yes": None,
            "book": None,
            "volume_usd": None,
            "url": None,
        }
    payload = row.get("payload")
    return {
        "id": ref,
        "question": row.get("question") or title,
        "venue": venue,
        "implied_yes": implied_yes_from_payload(payload),
        "book": book_from_payload(payload),
        "volume_usd": row.get("volume_usd"),
        "url": row.get("url"),
    }


def _cross_venue(
    k_block: dict[str, Any], pm_block: dict[str, Any], *, bet_type: str | None = None
) -> dict[str, Any]:
    """Build the cross_venue spread block."""
    cv: dict[str, Any] = {}
    ki = k_block.get("implied_yes")
    pi = pm_block.get("implied_yes")
    if ki is not None:
        cv["kalshi_implied"] = ki
    if pi is not None:
        cv["pm_implied"] = pi
    if ki is not None and pi is not None:
        # Mid-spread: kept for back-compat, but it OVERSTATES — a wide one-sided
        # book shows a big |mid diff| that isn't tradeable. `net_edge` below is
        # the honest, executable, fee-netted number; rank on that.
        cv["spread"] = round(ki - pi, 4)
    # Executable locked-arb edge (buy YES cheap + buy NO dear, net of Kalshi fee).
    # Positive = a real arb; ≤0 = the mid-spread was a phantom. Only emitted when
    # BOTH legs have a two-sided book. Mirrors t_find_divergences so the radar and
    # the scanner agree.
    net = locked_arb_net_edge(k_block.get("book"), pm_block.get("book"), bet_type=bet_type)
    if net is not None:
        cv["net_edge"] = net
        cv["executable"] = net > 0
    return cv


def _build_sort_key(sort_by: str) -> Any:
    """Return a sort key function for the given sort_by mode."""
    if sort_by == "spread":
        def _spread_key(p: dict[str, Any]) -> tuple[bool, float]:
            cv = p.get("cross_venue") or {}
            ki = cv.get("kalshi_implied")
            pi = cv.get("pm_implied")
            if ki is None or pi is None:
                # Missing either side → sort last (False < True when reversed)
                return (False, 0.0)
            return (True, abs(ki - pi))
        return _spread_key

    if sort_by == "net_edge":
        # The HONEST arb radar: rank by executable, fee-netted edge. Pairs without
        # a computable net_edge (one leg unpriced / one-sided book) sort last, so
        # the top rows are real, tradeable edges — not mid-spread phantoms.
        def _net_edge_key(p: dict[str, Any]) -> tuple[bool, float]:
            ne = (p.get("cross_venue") or {}).get("net_edge")
            if ne is None:
                return (False, 0.0)
            return (True, ne)
        return _net_edge_key

    if sort_by == "confidence":
        def _conf_key(p: dict[str, Any]) -> float:
            return p.get("confidence") or 0.0
        return _conf_key

    # Default: volume
    def _volume_key(p: dict[str, Any]) -> tuple[bool, float, float]:
        vol = p["kalshi"].get("volume_usd") or 0.0
        conf = p.get("confidence") or 0.0
        has_vol = p["kalshi"].get("volume_usd") is not None
        return (has_vol, vol, conf)
    return _volume_key


# In-flight concurrency cap: /v1/markets/matched is latency-bound (full-index scan +
# hydration) and NEVER trips the per-IP req/s limiter — 40 concurrent callers just see
# 10s+ p99s (2026-07-03 stress ramp). Shed load instead: beyond N in-flight requests,
# fast-fail 429 with Retry-After so clients back off cleanly like every other endpoint.
_MATCHED_MAX_INFLIGHT = 8
_matched_inflight = 0


async def handle_markets_matched(
    query: dict[str, str],
    *,
    dao: Any,
    equivalence: Any = None,
    force_refresh: bool = False,
) -> tuple[int, dict[str, Any]]:
    """GET /v1/markets/matched handler.

    ``equivalence`` accepts an EquivalenceIndex (or duck-typed equivalent with
    ``.browse()`` / ``.bet_types_available`` / ``.BET_TYPE_GROUPS`` /
    ``.pairs_loaded``). Defaults to the module-level singleton.
    """
    global _matched_inflight
    if _matched_inflight >= _MATCHED_MAX_INFLIGHT:
        # Latency-bound endpoint: shed load with a clean 429 instead of a 10s+ pile-up
        # (2026-07-03 stress ramp: never trips the per-IP req/s limiter; degrades silently).
        return 429, {"error": "rate_limited", "detail": "matched: concurrency limit",
                     "retry_after": 1}
    _matched_inflight += 1
    try:
        return await _handle_markets_matched_inner(query, dao=dao, equivalence=equivalence,
                                                   force_refresh=force_refresh)
    finally:
        _matched_inflight -= 1


async def _handle_markets_matched_inner(
    query: dict[str, str],
    *,
    dao: Any,
    equivalence: Any = None,
    force_refresh: bool = False,
) -> tuple[int, dict[str, Any]]:
    if equivalence is None:
        from pytheum.equivalence.index import get_index
        equivalence = get_index()

    limit = parse_limit(query, default=50, max_limit=200)
    offset = _parse_offset(query)
    min_vol = _parse_min_volume(query)
    sort_by = _parse_sort_by(query)
    fungible_only = _parse_fungible_only(query)

    # Resolve bet_type filter (group names expanded to concrete set).
    bet_type_param = query.get("bet_type") or query.get("bet_types")
    groups: dict[str, set[str]] = getattr(equivalence, "BET_TYPE_GROUPS", {})
    available_set: set[str] = set(getattr(equivalence, "bet_types_available", []))
    bet_types_filter = _parse_bet_type_filter(
        bet_type_param,
        groups=groups,
        available=available_set,
    )

    query_substr = query.get("q") or None
    league_filter = _parse_league(query)
    date_filter = _parse_game_date(query)

    # Param-keyed cache check (after parse so the key reflects NORMALIZED values:
    # the bet_type/bet_types alias collapses to one sorted concrete set, sort_by
    # falls back, dates parse the same, fungible_only/min_volume normalize). Built
    # from every param that shapes the result so two requests with different
    # params never collide and two identical requests hit. The bet_types set is
    # sorted so member-order doesn't fork the key. Only successful non-degraded
    # bodies are stored below (see the file_missing/load_error guard before set).
    cache_key = (
        f"limit={limit}"
        f"|offset={offset}"
        f"|min_volume={min_vol}"
        f"|sort_by={sort_by}"
        f"|fungible_only={fungible_only}"
        f"|bet_types={','.join(sorted(bet_types_filter)) if bet_types_filter else ''}"
        f"|q={query_substr or ''}"
        f"|league={league_filter or ''}"
        f"|date={date_filter or ''}"
    )
    if not force_refresh:
        hit = _matched_cache.get(cache_key)
        if hit is not None:
            return 200, hit

    # When fungible_only or min_vol is active we need the full filtered list to
    # paginate and count excluded; over-fetch with no internal limit cap.
    _overfetch = fungible_only or (min_vol is not None)
    _browse_kwargs: dict[str, object] = dict(
        bet_types=bet_types_filter,
        query_substr=query_substr,
        league=league_filter,
        game_date=date_filter,
        fungible_only=fungible_only,
    )

    # Browse the index — this is a pure in-memory O(n) scan.
    rows, total_filtered = equivalence.browse(
        **_browse_kwargs,
        limit=limit * 10 if _overfetch else limit,
        offset=0 if _overfetch else offset,
    )

    # Compute how many rows were excluded by the fungible_only filter.
    fungible_excluded: int = 0
    if fungible_only:
        _, total_without_fungible = equivalence.browse(
            bet_types=bet_types_filter,
            query_substr=query_substr,
            league=league_filter,
            game_date=date_filter,
            fungible_only=False,
            limit=1,
            offset=0,
        )
        fungible_excluded = total_without_fungible - total_filtered

    # Normalize refs and collect all IDs for a single batch fetch (N+1 fix).
    # Two sequential per-pair fetch_market calls (O(n) round-trips) are replaced
    # by one fetch_markets_by_ids call that retrieves all rows in one query.
    normalized_pairs: list[tuple[str | None, str | None, dict[str, Any]]] = []
    all_ids: list[str] = []
    seen_ids: set[str] = set()
    for pair in rows:
        k_ref = pair.get("kalshi_ref") or pair.get("kalshi_ticker")
        if k_ref and not k_ref.startswith("kalshi:"):
            k_ref = f"kalshi:{k_ref}"
        pm_ref = pair.get("pm_ref")
        normalized_pairs.append((k_ref, pm_ref, pair))
        for ref in (k_ref, pm_ref):
            if ref and ref not in seen_ids:
                seen_ids.add(ref)
                all_ids.append(ref)

    # Batch-fetch all market rows in a single DAO call (best-effort).
    market_cache: dict[str, dict[str, Any]] = {}
    with contextlib.suppress(Exception):
        fetched = await dao.fetch_markets_by_ids(all_ids)
        market_cache = {row["id"]: row for row in fetched}

    # Hydrate each pair from the in-memory cache.
    pairs: list[dict[str, Any]] = []
    for k_ref, pm_ref, pair in normalized_pairs:
        k_row = market_cache.get(k_ref) if k_ref else None
        pm_row = market_cache.get(pm_ref) if pm_ref else None

        # min_volume filter: skip only when we have a hydrated volume that is
        # below the threshold.
        if min_vol is not None:
            k_vol = k_row.get("volume_usd") if k_row else None
            if k_vol is not None and k_vol < min_vol:
                continue

        k_block = _hydrate_side(
            k_ref, pair.get("kalshi_title"), "kalshi", k_row
        )
        pm_block = _hydrate_side(
            pm_ref, pair.get("pm_title"), "polymarket", pm_row
        )
        cv = _cross_venue(k_block, pm_block, bet_type=pair.get("bet_type"))

        # live = both legs currently active in the store. Settled markets keep
        # their lifetime volume_usd, so a pure volume sort ranks dead 2022-24
        # pairs above live ones; is_live is the primary sort key (below) so the
        # default surfaces tradeable pairs first. Exposed so callers can filter.
        is_live = (
            (k_row or {}).get("status") == "active"
            and (pm_row or {}).get("status") == "active"
        )

        pairs.append({
            "kalshi": k_block,
            "polymarket": pm_block,
            "bet_type": pair.get("bet_type"),
            "confidence": pair.get("confidence"),
            "method": pair.get("method"),
            "cross_venue": cv,
            "is_live": is_live,
        })

    # When min_volume or fungible_only filtering was applied we over-fetched;
    # apply pagination now.
    if _overfetch:
        total_filtered = len(pairs)
        pairs = pairs[offset: offset + limit]

    # Sort by the requested mode, but ALWAYS rank live pairs (both legs active)
    # first — settled markets retain lifetime volume and would otherwise
    # dominate every sort mode with dead pairs.
    _inner = _build_sort_key(sort_by)
    pairs.sort(key=lambda p: (p.get("is_live", False), _inner(p)), reverse=True)

    # Meta block
    filter_block: dict[str, Any] = {
        "bet_type": bet_type_param,
        "q": query_substr,
        "min_volume": min_vol,
        "sort_by": sort_by,
        "fungible_only": fungible_only,
        "limit": limit,
        "offset": offset,
    }
    if league_filter is not None:
        filter_block["league"] = league_filter
    if date_filter is not None:
        filter_block["date"] = date_filter
    # Surface a silently-ignored sort_by: an unknown value falls back to "volume",
    # so echo what was requested when it differs from what was applied — an agent
    # that typo'd e.g. "arbitrage" shouldn't be misled into trusting a volume sort.
    requested_sort = (query.get("sort_by") or "").strip().lower()
    if requested_sort and requested_sort != sort_by:
        filter_block["sort_by_requested"] = requested_sort

    meta: dict[str, Any] = {
        "pairs_loaded": equivalence.pairs_loaded,
        "bet_types_available": list(getattr(equivalence, "bet_types_available", [])),
        "filter": filter_block,
    }
    if fungible_only:
        meta["fungible_excluded"] = fungible_excluded

    # leagues_available: emit when any rows in the dataset carry a league field.
    leagues_fn = getattr(equivalence, "leagues_available", None)
    if leagues_fn is not None:
        leagues = leagues_fn(max_values=50)
        if leagues:
            meta["leagues_available"] = leagues

    if getattr(equivalence, "file_missing", False):
        meta["degraded"] = True
        meta["degraded_reason"] = "equivalence_file_not_found"
    elif getattr(equivalence, "load_error", None):
        meta["degraded"] = True
        meta["degraded_reason"] = equivalence.load_error

    body = {
        "pairs": pairs,
        "total": total_filtered,
        "meta": meta,
    }
    # Only cache successful, non-degraded results — a degraded body (equivalence
    # file missing / load error) reflects a transient boot/load problem we don't
    # want to pin for the TTL once the index recovers.
    if not meta.get("degraded"):
        _matched_cache.set(cache_key, body)
    return 200, body
