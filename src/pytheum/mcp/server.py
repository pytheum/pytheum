"""MCP server exposing the pytheum context tools over two transports:

- stdio (`main`)  — local install path (pip/npm-launched).
- streamable-http (`http_main`) — the REMOTE CONNECTOR: hosted at /mcp behind
  Caddy so users add a URL with zero install (Claude web/desktop/mobile). Ships
  with a per-IP token-bucket rate limit so a free public endpoint can't be
  abused into unbounded OpenAI/Pinecone cost.

WARNING — PER-PROCESS RATE LIMITER:
    ``_buckets`` is a module-level dict; rate-limit state is NOT shared across
    OS processes.  The service MUST run single-process so all requests hit the
    same state.  Multi-worker deploys would allow (workers × per_IP_limit)
    effective req/min per IP, defeating the rate limit entirely.

    Current single-process guarantee: ``http_main()`` calls
    ``uvicorn.run(app, ...)`` with no ``workers=`` override, so uvicorn
    defaults to ``workers=1``.  Do NOT add ``workers=`` or switch to
    ``uvicorn.run(..., workers=N)`` without adding a shared store (e.g. Redis).
"""
from __future__ import annotations

import asyncio
import contextvars
import hashlib
import json
import os
import time
from typing import Any

from mcp.server.fastmcp import FastMCP

from pytheum.mcp.envelope import enveloped, set_usage_hook
from pytheum.mcp.guide import agent_about, agent_guide
from pytheum.mcp.tools import (
    bundle_context,
    context_batch,
    equivalent_markets,
    event_related_markets,
    find_divergences,
    find_markets,
    get_market,
    leaderboard,
    market_context,
    market_flow,
    market_history,
    market_holders,
    market_rules,
    matched_pairs,
    mm_reference,
    ohlcv,
    open_interest,
    orderbook,
    recent_trades,
    related_markets,
    screen_markets,
    search_markets,
    service_quality,
    service_status,
    trader_profile,
    whale_trades,
)

DEFAULT_BASE = os.environ.get("PYTHEUM_API_BASE", "https://api.pytheum.com")

# Surfaced by MCP clients on connect (the auto-read server instructions). Kept
# lean: orient a cold agent, then point at the onboarding tools for the rest.
_INSTRUCTIONS = (
    "Pytheum is the information substrate for prediction-market and forecasting "
    "agents: a settlement-verified cross-venue equivalence graph across Kalshi and "
    "Polymarket, plus a Hyperliquid correlated/related tier for cross-venue hedge "
    "discovery (Manifold is discovery-only), with every market paired to fresh "
    "real-time world context. An "
    "agent here gets fresher context than classic web search. New to this server? "
    "Call t_guide for the full tool playbook and conventions, t_status for live "
    "coverage and freshness, t_quality for data integrity, and t_about for the "
    "mission and who is building this. Market refs are <venue>:<id>, for example "
    "kalshi:KXFED-24DEC-525."
)

mcp = FastMCP("pytheum", instructions=_INSTRUCTIONS)


@mcp.tool()
@enveloped
async def t_status() -> dict:
    """Service health + dataset summary snapshot — keyless, no auth required. Returns `platforms` (per-venue market count + last_updated + "ok"/"stale" freshness indicator; omitted when the server lacks DAO-backed venue stats), `equivalence` (pairs_loaded + dataset_version for the cross-venue matcher gold set), `related` (pairs_loaded for the correlated-not-equivalent tier), and `service` (version + now). Use as a first call to confirm the service is up and the dataset is fresh before issuing market queries."""
    return await service_status(base_url=DEFAULT_BASE)


@mcp.tool()
@enveloped
async def t_market_context(market_ref: str, limit: int = 25) -> dict:
    """Events (news/social/macro) paired with a specific market. Per-leg `flow_flag` on outcomes/bundle_children/sibling_markets is a PRECOMPUTED positioning snapshot that can LAG live wallet flow (the refresh sidecar is paused, #223) and may disagree with t_market_flow — treat it as a coarse breadcrumb and confirm current direction with t_market_flow before trading. `market_ref` MUST be venue-prefixed — 'kalshi:KXNBA-26-NYK', 'polymarket:558936', or a full market URL (a bare 'KXNBA-26-NYK' / '558936' is rejected with a hint). Works on an OUTCOME-market leg (best — market-specific context + sibling_markets) OR a bundle/event PARENT (returns event-level context + the outcome ladder; t_bundle_context is the dedicated bundle view). On a bad/typo'd id you get {error, hint}, never a silent null."""
    return await market_context(market_ref, base_url=DEFAULT_BASE, limit=limit)


@mcp.tool()
@enveloped
async def t_bundle_context(bundle_ref: str, limit: int = 50) -> dict:
    """Events paired with any market inside a bundle, deduplicated by event_id. Per-leg `flow_flag` is a PRECOMPUTED positioning snapshot that can LAG live wallet flow (refresh sidecar paused, #223) — coarse breadcrumb; confirm with t_market_flow. `bundle_ref` is a GROUP/event id — 'polymarket:soccer', 'polymarket:2028-presidential-election', 'kalshi:KXNBA-26' — NOT a single market (for one market use t_market_context). Find bundle ids via the `bundle_id` field on t_screen/t_find_markets rows. Bad ref → {error, hint}."""
    return await bundle_context(bundle_ref, base_url=DEFAULT_BASE, limit=limit)


@mcp.tool()
@enveloped
async def t_find_markets(query: str, limit: int = 50, group_by: str | None = None,
                         venue: str | list[str] | None = None, min_similarity: float | None = None,
                         exclude_stale: bool = False) -> dict:
    """Find prediction markets matching a free-form text query (article body / news headline / question). Rows carry implied_yes/book/liquidity/resolution/resolution_status/condition_id/event_key/is_play_money; crypto rows also carry spot_ref (live underlying USD spot so you needn't leave pytheum to price a barrier). `venue` values are kalshi | polymarket | manifold (case-insensitive; accepts a string, comma-list, or array; aliases like "poly" and "all"/"both"→all venues work; an unknown venue returns an error, not an empty list); omit for all venues. `group_by` is 'bundle' (default, dedups to one row per event) or 'none'; `min_similarity` is a 0.0–1.0 cosine threshold (out of range → error). `exclude_stale=true` drops resolved/expired markets."""
    return await find_markets(query, base_url=DEFAULT_BASE, limit=limit, group_by=group_by,
                              venue=venue, min_similarity=min_similarity,
                              exclude_stale=exclude_stale)


@mcp.tool()
@enveloped
async def t_event_related_markets(event_id: str, limit: int = 25) -> dict:
    """Markets related to a specific firehose `event_id` (looks like 'evt_news_headline_…', from the events paired in t_market_context or the live stream) — NOT a market_ref. Only events within the 24h rolling window resolve. Passing a market_ref returns {error, hint} pointing you to t_market_context."""
    return await event_related_markets(event_id, base_url=DEFAULT_BASE, limit=limit)


@mcp.tool()
@enveloped
async def t_market_history(market_ref: str, limit: int = 500, full: bool = False) -> dict:
    """PIT price+book history + derived moves (move_1h/24h/7d, each present only when the series spans that window) for a market — pytheum's own point-in-time capture; tells you if a price is stale. Returns `staleness` (last_observed_age_s = snapshot freshness; last_move_age_s = seconds the price has been frozen — a stale-quote-trap flag; is_live_event = moved recently + near-dated => cross-venue gaps are latency, not arb) computed over the FULL series. By DEFAULT the `points` array is DOWNSAMPLED (~40 evenly-spaced, newest kept) so the response stays small — for most "is this stale / how did it move" questions the staleness+moves block is all you need. Pass full=true ONLY when you need the complete tape (e.g. plotting); it can be 500+ points and large. `points_total` is the full count; `downsampled` flags the thinning. Works for Polymarket + Kalshi. `market_ref` must be a venue-prefixed OUTCOME-market id ('kalshi:KXNBA-26-NYK', 'polymarket:558936') — a bundle/event PARENT has no own price series (explanatory hint, not a silent empty). `limit` (≥1, capped 2000) bounds the underlying series."""
    return await market_history(market_ref, base_url=DEFAULT_BASE, limit=limit, full=full)


@mcp.tool()
@enveloped
async def t_market_flow(market_ref: str, window_hours: int = 24) -> dict:
    """Wallet-level trade flow for a Polymarket market: net directional pressure, whale concentration, largest recent positions. coverage is "tracked" (stored aggregate; timing_anomalies not yet active) or "on_demand" (live snapshot). Polymarket only — a Kalshi ref returns coverage:"unavailable" with a reason. Pass an outcome-market leg with a conditionId (a bundle parent has none). `window_hours` is clamped to 1–168."""
    return await market_flow(market_ref, base_url=DEFAULT_BASE, window_hours=window_hours)


@mcp.tool()
@enveloped
async def t_find_divergences(min_net_edge: float = 0.0, limit: int = 10,
                              include_rules: bool = True,
                              fungible_only: bool = False,
                              include_warned: bool = True,
                              include_depth: bool = True) -> dict:
    """Cross-venue divergence scanner: VERIFIED same-question Kalshi↔Polymarket pairs (the matcher's pre-decided gold set — 140k+ pairs, served not re-matched) joined to live books and sorted CLEAN-FIRST, then by ANNUALIZED net-of-fees locked edge (capital efficiency — a 2.26% lock over 887d sorts below a 1% lock resolving next week), in one call. Each pair carries net_edge, annualized_net_edge, lock_days, `matched_by` (the method that decided the pair: spread_match / game_title_match / human_adjudicated / …; deterministic methods carry null match_confidence by design) + title_similarity, `either_leg_parked` (true = one leg's quote is a frozen parked wall — see each leg's is_parked_wall/last_move_age_s — so the edge is a ghost, not a live lock), a first-class `warnings` list (resolution_mismatch / either_leg_parked / stale_quote (a leg frozen >1h) / near_resolution (a leg <1 day out) / depth_unverified), and `max_lockable_notional` (depth-capped fillable USD at the quoted top-of-book, min over legs of executable-side size × price; null + depth_unverified when a leg's size is unknown — never guessed; `notional_basis` documents the computation). A LARGE EDGE WITH WARNINGS IS USUALLY QUOTE NOISE — read `warnings` before acting: any warned row sorts AFTER every clean row (edge order kept within each group). `include_warned` (default true) keeps warned rows in the response (demoted + labeled); false filters them out (`warned_filtered` counts them). Real-money only (Manifold excluded). `min_net_edge` (e.g. 0.03) filters on raw net edge. `include_rules` (default true) bundles settlement rules text for each pair as `resolution.kalshi` / `resolution.polymarket` (400-char truncated) — saves a t_market_rules round-trip per pair. `fungible_only` (default false) restricts to deterministic/structural pairs, excluding LLM-judged matches. `include_depth` (default true) runs a PAGE-LOCAL live-depth overlay: after the final sort + limit slice, both legs' live top-of-book sizes are fetched concurrently (≤ 2×limit coalesced GETs, ~4s overall deadline; rows beyond the page are NOT fetched) so `max_lockable_notional` actually populates — sized rows get a live-depth `notional_basis`, lose their depth_unverified warning, and the page is re-sorted (a row that became clean floats above warned rows within the page); a failed leg keeps its honest null; `depth_overlaid` counts updated rows; false skips all book fetches. Coverage grows as the equivalence loader re-runs; legs missing from our market snapshot are absent, not guessed."""
    return await find_divergences(base_url=DEFAULT_BASE, min_net_edge=min_net_edge,
                                  limit=limit, include_rules=include_rules,
                                  fungible_only=fungible_only,
                                  include_warned=include_warned,
                                  include_depth=include_depth)


@mcp.tool()
@enveloped
async def t_matched_pairs(
    bet_type: str | None = None,
    query: str | None = None,
    sort_by: str = "volume",
    limit: int = 25,
    league: str | None = None,
    date: str | None = None,
    fungible_only: bool = False,
) -> dict:
    """Browse pytheum's verified cross-venue matched pairs (Kalshi<->Polymarket, 140k+ settlement-verified pairs). Filter by bet_type (e.g. 'sports' group, or specific: moneyline,total,spread,tennis_ml,...) and/or free-text query over titles; returns both venues' live prices per pair, plus a `cross_venue` block with the mid `spread` AND an executable, fee-netted `net_edge` (+ `executable` bool). **sort_by='net_edge' is the HONEST arb radar** — ranks by the real locked-arb edge after Kalshi + Polymarket taker fees (Polymarket began charging category-dependent taker fees in 2026), pairs without a two-sided book on both legs sort last. sort_by='spread' ranks by mid |price diff| but OVERSTATES one-sided books (a wide book shows a big spread that dies on execution) — prefer net_edge. sort_by='volume' (default) or 'confidence' also accepted. `league` (e.g. 'NBA', 'NFL') filters to rows with a matching league field (rows without league are excluded); `date` (YYYY-MM-DD) filters to rows with a matching game_date field (rows without game_date are excluded); `meta.leagues_available` lists available leagues (≤50) when the dataset has league data. `fungible_only=true` restricts to deterministic/structural pairs (no LLM-judged matches); meta.fungible_excluded reports how many pairs were excluded. Use t_equivalent_markets for a single known market."""
    return await matched_pairs(
        bet_type=bet_type,
        query=query,
        sort_by=sort_by,
        limit=limit,
        league=league,
        date=date,
        fungible_only=fungible_only,
        base_url=DEFAULT_BASE,
    )


@mcp.tool()
@enveloped
async def t_equivalent_markets(market_ref: str) -> dict:
    """Find the SAME market on the other venue (Kalshi<->Polymarket) from pytheum's verified 140k+-pair equivalence dataset, with both venues' live prices and the cross-venue spread. The pairs are settlement-verified (same event, same resolution semantics), not fuzzy title matches. `market_ref` must be venue-prefixed — 'kalshi:KXFED-25-MAY', 'polymarket:558936', or a full market URL. Returns the queried market's metadata, a list of equivalents with live implied_yes/book/volume when available, a cross_venue block with kalshi_implied / pm_implied / spread (kalshi_implied minus pm_implied), and a meta block with pairs_loaded / dataset_version / matched_via. When the file is missing the response degrades (empty equivalents + meta.degraded=true) rather than erroring."""
    return await equivalent_markets(market_ref, base_url=DEFAULT_BASE)


@mcp.tool()
@enveloped
async def t_market_rules(market_ref: str) -> dict:
    """Resolution rules text for a market AND its settlement-verified cross-venue equivalent, side by side, with deadlines — exactly how each venue decides the outcome. Use before treating two venues' prices as comparable: small wording differences (strict-vs-inclusive thresholds, different settlement sources, deadline gaps) make seemingly identical markets resolve differently. `market_ref` must be venue-prefixed — 'kalshi:KXFED-25-MAY' or 'polymarket:558936'. Returns: `market` (focal market with full `resolution` rules text, `resolution_at`, `url`), `equivalent` (same fields for the verified counterpart; null if no cross-venue pair), `comparison` (deadlines.kalshi / deadlines.polymarket, same_deadline_day bool-or-null, confidence, method from the dataset), and `meta` (pairs_loaded, dataset_version, matched_via). When the focal market is unknown to the store but present in the equivalence index, titles are returned with null rules text."""
    return await market_rules(market_ref, base_url=DEFAULT_BASE)


@mcp.tool()
@enveloped
async def t_mm_reference(market_ref: str) -> dict:
    """Market-maker cross-venue REFERENCE for one verified pair: a consolidated fair value + a fungibility verdict + the risk inputs a quoting model consumes — the "data layer, not the edge" surface for a prediction-market market maker (SIG/Jump/IMC-class). Composes the settlement-verified equivalence pair's two legs (oriented prices + book from the equivalents endpoint) with their resolution rules, all server-assembled data, and runs a local analytics layer. `market_ref` must be venue-prefixed — 'kalshi:KXFED-25-MAY' or 'polymarket:558936'. Returns `mm_reference` {`p_hat` (the consolidated cross-venue fair value — a depth/tightness-weighted blend of both venues' mids, the 'NBBO of prediction markets'), `basis` (kalshi_implied − pm_implied, the informed cross-venue divergence), `fungibility` {fungible, confidence, method, reason} (is the pair SAFE to treat as ONE instrument / a hedge? deterministic/structural matches are fungible by construction; LLM-judged matches must clear a 0.90 confidence floor; a settlement divergence DETECTED from the two legs' rules text — a different numeric threshold or a different settlement source/oracle — VETOES even a confident match, the #1 MM risk in the 2026 settlement-dispute era), `risk_inputs` {terminal_variance = Bernoulli p(1−p), the 0/1-payoff variance that replaces diffusion σ² in a PM-adapted Avellaneda-Stoikov model; time_to_resolution_years = the real A-S horizon T}, `legs`, `warnings` (not_fungible / settlement_divergence / wide_basis / one_leg_missing / orientation_unknown / deadline_mismatch — what a maker must NOT do)}, an `illustrative_quote` (a flat-inventory A-S bid/ask around p_hat with PLACEHOLDER γ/κ — shows how the inputs feed the model; the maker calibrates its own; null unless fungible), a `pair` block (both refs + oriented implieds + method/confidence + orientation), and `meta`. Read-only: reference + fungibility, NOT quoting/sizing/execution — those are yours. Use t_equivalent_markets for just the twin + spread; t_market_rules for the raw side-by-side settlement text."""
    return await mm_reference(market_ref, base_url=DEFAULT_BASE)


@mcp.tool()
@enveloped
async def t_get_market(market_ref: str) -> dict:
    """Lean fetch of ONE market's CORE by ref — the fast "get this market" call when an agent lands with a venue id or a market URL and doesn't need the full t_market_context payload (probability ladder + sibling markets + fetched news). `market_ref` is venue-prefixed ('kalshi:KXFED-25-MAY', 'polymarket:558936', 'polymarket:0x<cond>', a slug) or a market URL; a raw Kalshi ticker also resolves. Returns `market` {id, venue, question, status, implied_yes, book (bid/ask/spread/sizes), volume_usd, condition_id, resolution_status, resolution_at, url, found} and `meta` {has_equivalent (true → drill into t_equivalent_markets for the cross-venue twin + spread), matched_via, pairs_loaded}. When the market isn't in the store, `market.found=false` + `meta.degraded` rather than an error. Use t_market_context instead when you need rules, the ladder, siblings, or news; t_find_markets/t_screen to discover by query/filter."""
    return await get_market(market_ref, base_url=DEFAULT_BASE)


@mcp.tool()
@enveloped
async def t_related_markets(market_ref: str, include_hyperliquid: bool = False) -> dict:
    """Correlated cross-venue markets that are NOT settlement-equivalent (different bands/sources/deadlines) — hedge discovery, not arbitrage. `market_ref` must be venue-prefixed — 'kalshi:KXFED-25-MAY' or 'polymarket:558936'. Returns a list of related markets, each carrying the relation type, both venues' bands, and a `basis` note spelling out exactly how settlement differs (so you don't mistake a correlated leg for a fungible hedge). `include_hyperliquid` (default false, opt-in) additionally attaches `hyperliquid_related`: venue-explicit rows from the Hyperliquid related tier — each row is a 2-element `legs` list (one hyperliquid leg + one kalshi-or-polymarket leg; every leg carries venue/ref/native_id/title, PM legs add gamma_id/slug, the HL leg adds implied_yes + as_of) plus relation/settlement/basis_note metadata — and `hyperliquid_note`: HL leg prices are a mint-time daily snapshot, NOT live quotes, so treat any cross-venue spread involving the HL leg as indicative, not executable. If the HL dataset file is absent you get `hyperliquid_related: []` + `hyperliquid_file_missing: true` (never an error). Use when you want a correlated position to contextualize or hedge a market but no exact same-question pair exists; use t_equivalent_markets for true same-market pairs."""
    return await related_markets(market_ref, base_url=DEFAULT_BASE,
                                 include_hyperliquid=include_hyperliquid)


@mcp.tool()
@enveloped
async def t_context_batch(market_refs: list[str], limit: int = 8) -> dict:
    """Batch DIGEST of t_market_context for up to 25 markets in ONE call (avoids N round trips). Each ref returns a LEAN digest sized so 25 real markets fit inline: a market CORE (id/question/venue/implied_yes/book-with-net-prices/volume_usd_norm/taker_fee_bps/flow_flag/days_to_resolution/is_stale/resolution_status/market_archetype) + up to 3 context headlines + sibling_markets_count / bundle_children_count. The full market object (resolution text, condition_id, …), the heavy sibling/leg lists, and full article bodies are omitted — drill into a single ref with t_market_context for those. `market_refs` is a non-empty list of venue-prefixed ids. Partial failures don't sink the batch — returns {results: {ref: ...}, count, ok_count, error_count}; a bad ref's entry is {error, hint}."""
    return await context_batch(market_refs, base_url=DEFAULT_BASE, limit=limit)


@mcp.tool()
@enveloped
async def t_screen(
    venues: str | list[str] | None = None,
    status: str = "active",
    min_volume: float | None = None,
    max_volume: float | None = None,
    min_liquidity: float | None = None,
    resolves_before: str | None = None,
    resolves_after: str | None = None,
    sort_by: str = "volume",
    limit: int = 50,
    exclude_stale: bool = False,
    full: bool = False,
) -> dict:
    """Structured (non-semantic) market screen — filter by venue/status/volume(min+max)/liquidity/resolution window, sort by volume|liquidity|resolution. `venues` values are kalshi | polymarket | manifold (case-insensitive string/comma-list/array; "all"/"both"→all venues; unknown venue → error not empty; omit for all). `resolves_before`/`resolves_after` must be ISO-8601 dates (a bad date errors instead of being silently ignored). `sort_by` is volume | liquidity | resolution | move (move = TOP MOVERS: ranks a top-300-volume pool by |move_24h| — 'what moved today' in one call; unknown → error, not a silent volume-sort); `status` is case-insensitive, defaults 'active' (common: active|resolved|closed; 'any'/'all'→all statuses); resolves_after later than resolves_before → empty_window error. `exclude_stale=true` drops resolved/expired markets still listed active. Rows carry implied_yes/book/resolution_status/condition_id and (for bundle/event parents) `bundle_top_outcome` (the favorite leg), plus quote-staleness inline: `last_move_age_s` (seconds since the price last changed) and `is_parked_wall` (true = quote frozen behind a tight spread on an active market — a resting limit order, NOT a live price; don't rank a cross-venue gap off it without confirming via t_market_history). `full=true` adds the complete `bundle_outcomes` ladder per parent (omitted by default to keep the page small — it's ~28% of the payload; the favorite is already in bundle_top_outcome). One call replaces N semantic searches. Rows carry move_24h/move_7d (live tape + tick-archive refs). Kalshi rows now carry live prices/book/volume/resolution for cross-venue comparison; crypto rows also carry spot_ref (live underlying USD spot)."""
    return await screen_markets(
        base_url=DEFAULT_BASE, venues=venues, status=status, min_volume=min_volume,
        max_volume=max_volume, min_liquidity=min_liquidity, resolves_before=resolves_before,
        resolves_after=resolves_after, sort_by=sort_by, limit=limit,
        exclude_stale=exclude_stale, full=full)


@mcp.tool()
@enveloped
async def t_search_markets(
    q: str,
    venue: str | list[str] | None = None,
    status: str = "active",
    limit: int = 50,
) -> dict:
    """Text search over market TITLES across both venues — the cheap, exact complement to t_find_markets' semantic search. AND-matches your query's title tokens (so 'super bowl winner' must contain all of super/bowl/winner) and ranks by volume; NON-semantic, so it nails exact terms a paraphrase-based kNN can miss (a ticker like KXBTC, a player name, 'H5N1') but will NOT find conceptual paraphrases — for "markets like this article/headline" use t_find_markets instead. Rows carry the same triage shape as t_screen (implied_yes/book/resolution/resolution_status/condition_id + the verified cross_venue twin + quote-staleness) so you can size an edge without a t_get_market round-trip. `q` is required (non-empty). `venue` is kalshi | polymarket | manifold (case-insensitive string/comma-list/array; aliases like "poly" and "all"/"both"→all venues; unknown venue → error not empty; omit for all). `status` defaults to 'active' (case-insensitive; 'any'/'all' → every status). `limit` 1–200, default 50. Keyless. An empty result carries a meta.hint distinguishing "no such title" from "you wanted a semantic match"."""
    return await search_markets(q, base_url=DEFAULT_BASE, venue=venue, status=status, limit=limit)


@mcp.tool()
@enveloped
async def t_orderbook(market_ref: str, depth: int = 20) -> dict:
    """Live orderbook snapshot for a market — direct venue fetch, coalesced+cached ~2 s server-side (concurrent requests for the same key share ONE underlying call). Returns bids/asks as [[price, size], ...] in probability units [0,1] plus a top-of-book summary (bid, ask, spread, mid, sizes). ``market_ref`` must be venue-prefixed ('kalshi:KXNBA-26-NYK' or 'polymarket:some-slug'). depth 1–200, default 20. Read-only: no trading keys, no order submission. On any venue error returns source:"unavailable" instead of raising."""
    return await orderbook(market_ref, depth=depth, base_url=DEFAULT_BASE)


@mcp.tool()
@enveloped
async def t_recent_trades(market_ref: str, limit: int = 50) -> dict:
    """Recent trade tape for a market — live venue fetch, coalesced+cached ~10 s. Returns {trades: [{ts, price, size, side}, ...], count, venue, source:"live"}. market_ref must be venue-prefixed. limit 1–1000, default 50. Read-only: no trading keys. On venue error returns source:"unavailable"."""
    return await recent_trades(market_ref, limit=limit, base_url=DEFAULT_BASE)


@mcp.tool()
@enveloped
async def t_ohlcv(
    market_ref: str,
    interval: str = "1h",
    since: str | None = None,
    until: str | None = None,
    limit: int = 200,
) -> dict:
    """OHLCV candles for any Kalshi/Polymarket market — pytheum's own point-in-time capture first (no lookahead, backtest-grade), venue candles as fallback; source is disclosed per response. `interval` is one of 1m|5m|15m|1h|1d (default 1h); `since`/`until` are ISO-8601 or Unix-second timestamps (default: last 7 days); `limit` 1–1000 (default 200) caps candle count. `market_ref` must be venue-prefixed ('kalshi:KXFED-25-MAY' or 'polymarket:558936'). Response: {market: {id, question, venue}, interval, candles: [{t, o, h, l, c, v}], meta: {source: pit_archive|venue_live|mixed, count, partial_last_bucket}}. `v` is null when no trade-count data is available. An invalid interval/range returns {error, hint} rather than raising."""
    return await ohlcv(
        market_ref,
        interval=interval,
        since=since,
        until=until,
        limit=limit,
        base_url=DEFAULT_BASE,
    )


@mcp.tool()
@enveloped
async def t_open_interest(market_ref: str) -> dict:
    """Current open interest (total contracts/shares outstanding) for a market — use to gauge how much capital is committed and whether real depth backs a quote. Live venue fetch, coalesced+cached ~30 s. Returns {open_interest: float|null, venue, ref, source:"live"}. market_ref must be venue-prefixed. Read-only: no trading keys. On venue error returns source:"unavailable"."""
    return await open_interest(market_ref, base_url=DEFAULT_BASE)


@mcp.tool()
@enveloped
async def t_leaderboard(period: str = "weekly") -> dict:
    """Polymarket trader leaderboard — live venue fetch, coalesced+cached 300 s. Returns ranked traders with profit/volume stats: {period, traders: [{name, address, profit, volume, rank}], count, source, venue}. period is 'weekly' or 'monthly'. Polymarket-only: Kalshi trades are fully anonymized — no equivalent trader ranking exists on that venue. On any venue error returns source:"unavailable"."""
    return await leaderboard(period=period, base_url=DEFAULT_BASE)


@mcp.tool()
@enveloped
async def t_trader_profile(wallet: str) -> dict:
    """Polymarket trader profile — positions, recent activity, and portfolio value merged in one call. Live venue fetch, coalesced+cached 60 s. wallet is a 0x-hex address or Polymarket username. Returns {wallet, positions[], activity[], value, meta}. Polymarket-only: Kalshi trades are anonymized. On any venue error returns source:"unavailable"."""
    return await trader_profile(wallet, base_url=DEFAULT_BASE)


@mcp.tool()
@enveloped
async def t_market_holders(market_ref: str) -> dict:
    """Holder breakdown for a Polymarket market — who holds YES/NO tokens and how much. market_ref must be venue-prefixed 'polymarket:…'. Live venue fetch, coalesced+cached 60 s. Polymarket-only: Kalshi trades are anonymized — no holder breakdown exists. Returns {holders: [{address, amount, outcome}], count, ref, source, venue}. On any venue error returns source:"unavailable"."""
    return await market_holders(market_ref, base_url=DEFAULT_BASE)


@mcp.tool()
@enveloped
async def t_whale_trades(min_usd: float = 500, limit: int = 50, market_ref: str | None = None) -> dict:
    """Large-notional Polymarket trades where notional_usd (size * price) >= min_usd. Live venue fetch, coalesced+cached 30 s. market_ref (optional, 'polymarket:…') filters to one market. Returns {trades: [{ts, market, price, size, notional_usd, side, wallet, pseudonym?}], count, min_usd, venue, source}. Polymarket-only: Kalshi trades are anonymized. On any venue error returns source:"unavailable"."""
    return await whale_trades(min_usd=min_usd, limit=limit, market_ref=market_ref, base_url=DEFAULT_BASE)


@mcp.tool()
@enveloped
async def t_quality() -> dict:
    """Dataset quality + integrity transparency — keyless, no auth. The "verify before you pay" artifact, all DERIVED from the shipped equivalence dataset (no asserted numbers). Returns: `pairs_total` + `dataset_version`; `tiers` (fungible = deterministic/structural/human-reviewed settlement-verified vs judged = LLM-adjudicated, each with pairs + pct); `by_method` / `by_bet_type` composition + `bet_types_total`; `integrity` (the build-time invariants enforced before the dataset ships — 1:1, single-slice-per-id, line-invariant, abbrev/name-alignment, same-city); and `precision` (per-tier posture; `audited_pct` is null on purpose — a labeled-sample precision %% is published separately, not asserted here). Use to show a buyer/agent exactly how much of the gold set is structurally-guaranteed vs LLM-judged before trusting a pair."""
    return await service_quality(base_url=DEFAULT_BASE)


@mcp.tool()
@enveloped
async def t_guide() -> dict:
    """Self-onboarding playbook for an agent landing on pytheum cold — CALL THIS FIRST if you're unsure where to start. Local, no network. Returns: `summary` (what pytheum is), `principles` (operating rules — e.g. market_ref must be venue-prefixed; equivalence is the core; confirm settlement+staleness before trading a spread; this server is read-only), `conventions` (the market_ref format + the {ok,command,data,meta} response-envelope contract every tool returns), `tool_groups` (the full tool inventory grouped by job: health / discover / market_detail / cross_venue_equivalence / microstructure / flow_and_traders / events_and_batch), and `workflows` (ordered step recipes for common goals: find+validate a cross-venue arb, check if a market exists on the other venue, research one market, find today's movers)."""
    return agent_guide()


@mcp.tool()
@enveloped
async def t_about() -> dict:
    """Who Pytheum is, what the data covers, why it exists, and who is building it."""
    return agent_about()


def main() -> None:
    asyncio.run(mcp.run_stdio_async())


# --------------------------------------------------------------------------
# Remote connector: streamable-http transport + per-IP token-bucket rate limit
# --------------------------------------------------------------------------
_RL_PER_MIN = float(os.environ.get("PYTHEUM_MCP_RL_PER_MIN", "60"))   # sustained req/min/IP
_RL_BURST = float(os.environ.get("PYTHEUM_MCP_RL_BURST", "60"))       # bucket size
_buckets: dict[str, list[float]] = {}  # ip -> [tokens, last_monotonic]


def _client_ip(scope: dict) -> str:
    for k, v in scope.get("headers", []):
        if k == b"x-forwarded-for":  # Caddy sets this; take the first hop
            return v.decode("latin-1").split(",")[0].strip()
    client = scope.get("client")
    return client[0] if client else "unknown"


# --------------------------------------------------------------------------
# Per-tool usage emitter (feeds per-tool traction). One best-effort JSONL line
# per tool call: {"ts", "tool", "ip_hash"}. Fully non-blocking + swallows every
# failure — a logging problem must NEVER affect a tool call.
# --------------------------------------------------------------------------
_USAGE_LOG = os.environ.get("PYTHEUM_USAGE_LOG", "/var/log/pytheum/tool_usage.jsonl")
_USAGE_SALT = os.environ.get("PYTHEUM_USAGE_SALT", "pytheum-usage-v1")
# The current request's client IP, set by the ASGI rate-limit wrapper (which
# already extracts it) and read at tool dispatch. A ContextVar is loop- and
# task-safe; defaults to "unknown" when no request scope is in flight.
_current_ip: contextvars.ContextVar[str] = contextvars.ContextVar(
    "pytheum_current_ip", default="unknown")


def _ip_hash(ip: str) -> str:
    return hashlib.sha256((_USAGE_SALT + ip).encode("utf-8")).hexdigest()[:16]


def _emit_usage(tool: str) -> None:
    """Append one usage event for ``tool`` to the usage log. Best-effort: any
    failure (unwritable path, missing dir, encoding) is silently swallowed."""
    try:
        line = json.dumps({"ts": time.time(), "tool": tool,
                           "ip_hash": _ip_hash(_current_ip.get())})
        with open(_USAGE_LOG, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except Exception:  # never let usage logging affect the tool call
        pass


def _allow(ip: str) -> bool:
    now = time.monotonic()
    tokens, last = _buckets.get(ip, (_RL_BURST, now))
    tokens = min(_RL_BURST, tokens + (now - last) * (_RL_PER_MIN / 60.0))
    if tokens < 1.0:
        _buckets[ip] = [tokens, now]
        return False
    if len(_buckets) > 50_000:
        # Per-key stale pruning: remove IPs whose bucket has fully refilled
        # since last use — their state is equivalent to a fresh entry, so
        # evicting them is safe.  A full _buckets.clear() would bypass the
        # rate window for all currently-active IPs, undoing their back-pressure.
        _refill_time = _RL_BURST / (_RL_PER_MIN / 60.0)
        stale = [k for k, v in _buckets.items() if now - v[1] > _refill_time]
        for k in stale:
            del _buckets[k]
        # Safety net: if we're still over the limit (e.g. a sudden burst of
        # distinct IPs none of which are stale yet), evict the longest-idle
        # entries rather than wiping all active windows.
        if len(_buckets) > 50_000:
            by_age = sorted(_buckets, key=lambda k: _buckets[k][1])
            for k in by_age[: len(_buckets) - 50_000]:
                del _buckets[k]
    _buckets[ip] = [tokens - 1.0, now]
    return True


# Browser-based MCP clients (the claude.ai / claude.com web "custom connector"
# flow) send a CORS preflight before connecting and must READ the
# `mcp-session-id` response header to continue the session. Without CORS the
# preflight 405s and the response origin is blocked, so the connector hangs
# forever on "Checking connection…" (works in curl, which ignores CORS). Allow
# any origin (this is a public, no-auth, drop-a-link connector by design) and
# expose the session header so browser clients can complete the handshake.
_CORS_ALLOW_ORIGINS = ["*"]
_CORS_ALLOW_METHODS = ["GET", "POST", "DELETE", "OPTIONS"]
_CORS_ALLOW_HEADERS = [
    "content-type", "authorization", "mcp-session-id", "mcp-protocol-version",
    "last-event-id",
]
_CORS_EXPOSE_HEADERS = ["mcp-session-id"]


def _build_http_app() -> Any:
    """Build the streamable-http MCP ASGI app: CORS (for browser connectors)
    wrapped by a thin IP rate-limit gate. Extracted from ``http_main`` so the
    CORS preflight behaviour is unit-testable without binding a port."""
    from starlette.middleware.cors import CORSMiddleware

    inner = CORSMiddleware(
        mcp.streamable_http_app(),
        allow_origins=_CORS_ALLOW_ORIGINS,
        allow_methods=_CORS_ALLOW_METHODS,
        allow_headers=_CORS_ALLOW_HEADERS,
        expose_headers=_CORS_EXPOSE_HEADERS,
        max_age=86400,
    )

    async def app(scope, receive, send):  # thin ASGI rate-limit wrapper
        if scope["type"] == "http":
            ip = _client_ip(scope)
            # Make the IP available to the per-tool usage emitter at dispatch.
            _current_ip.set(ip)
            if not _allow(ip):
                return await _send_rate_limited(send)
        await inner(scope, receive, send)  # http (allowed) + lifespan pass through

    return app


async def _send_rate_limited(send) -> None:
    """Emit the 429 (with CORS so a browser client doesn't see an opaque fail)."""
    await send({"type": "http.response.start", "status": 429,
                "headers": [(b"content-type", b"application/json"),
                            (b"retry-after", b"5"),
                            (b"access-control-allow-origin", b"*")]})
    await send({"type": "http.response.body",
                "body": b'{"error":"rate_limited","retry_after_s":5}'})


def http_main() -> None:
    """Serve the streamable-http MCP app (remote connector) with rate limiting."""
    import uvicorn
    from mcp.server.transport_security import TransportSecuritySettings

    # The MCP SDK's DNS-rebinding protection is localhost-only by default and
    # 421s the Caddy-proxied `Host: api.pytheum.com`. That protection guards a
    # browser being tricked into hitting localhost — N/A here: the service binds
    # 127.0.0.1 and Caddy (api.pytheum.com TLS) is the only ingress. Disable it.
    mcp.settings.transport_security = TransportSecuritySettings(
        enable_dns_rebinding_protection=False)

    # Best-effort per-tool usage tracking (remote connector only).
    set_usage_hook(_emit_usage)

    app = _build_http_app()
    port = int(os.environ.get("PYTHEUM_MCP_HTTP_PORT", "8444"))
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="info")


if __name__ == "__main__":
    main()
