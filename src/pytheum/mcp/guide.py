"""Self-onboarding playbook for an agent landing on the pytheum MCP cold.

``agent_guide()`` returns a compact, local (no-network) machine-readable brief:
the service's purpose, operating principles, conventions (market_ref format, the
response envelope), a tool inventory grouped by job, and step-by-step workflow
recipes. Surfaced as the ``t_guide`` MCP tool and the ``pytheum guide`` CLI.

The tool names referenced here are asserted against the live FastMCP registry in
tests (``test_mcp_guide.py``) so this playbook can never drift out of sync with
the tools actually served.
"""
from __future__ import annotations

from typing import Any

GUIDE_VERSION = 1

# Tool inventory grouped by the job an agent is trying to do. Every name here is
# cross-checked against the registered MCP tools in tests (no drift).
_TOOL_GROUPS: list[dict[str, Any]] = [
    {
        "group": "health",
        "tools": [
            {"name": "t_status", "use": "Service health + dataset freshness. Call FIRST."},
            {"name": "t_quality", "use": "Dataset quality/integrity: fungible-vs-judged tier split, enforced invariants, honest precision posture."},
            {"name": "t_guide", "use": "This playbook."},
            {"name": "t_about", "use": "Who Pytheum is, the mission/vision, and who is building it."},
        ],
    },
    {
        "group": "discover",
        "tools": [
            {"name": "t_find_markets", "use": "Semantic search from a query / headline / article body."},
            {"name": "t_search_markets", "use": "Exact title-token search (tickers, names) — the non-semantic complement."},
            {"name": "t_screen", "use": "Structured filter (venue/volume/liquidity/resolution); sort_by='move' = top movers."},
        ],
    },
    {
        "group": "market_detail",
        "tools": [
            {"name": "t_get_market", "use": "Lean core of one market by ref/URL."},
            {"name": "t_market_context", "use": "Market + paired news/social/macro + outcome ladder + siblings."},
            {"name": "t_bundle_context", "use": "Context across all markets in an event/bundle."},
            {"name": "t_market_rules", "use": "Resolution rules text for a market AND its cross-venue twin, side by side."},
            {"name": "t_market_history", "use": "PIT price/book history + staleness flags (is the quote frozen?)."},
            {"name": "t_ohlcv", "use": "OHLCV candles (PIT-first, backtest-grade; venue fallback)."},
        ],
    },
    {
        "group": "cross_venue_equivalence",
        "note": "The core asset — verified same-question Kalshi<->Polymarket pairs.",
        "tools": [
            {"name": "t_equivalent_markets", "use": "The SAME market on the other venue for one known ref + the spread."},
            {"name": "t_matched_pairs", "use": "Browse the verified matched pairs; sort_by='net_edge' = honest arb radar."},
            {"name": "t_find_divergences", "use": "Cross-venue divergence scanner: gold pairs joined to live books, ranked by annualized net-of-fees edge."},
            {"name": "t_related_markets", "use": "Correlated-but-NOT-equivalent markets (hedge discovery, not arbitrage)."},
        ],
    },
    {
        "group": "microstructure",
        "tools": [
            {"name": "t_orderbook", "use": "Live order book snapshot (bids/asks + top-of-book)."},
            {"name": "t_recent_trades", "use": "Recent trade tape."},
            {"name": "t_open_interest", "use": "Open interest — capital committed behind a quote."},
        ],
    },
    {
        "group": "flow_and_traders",
        "note": "Polymarket only — Kalshi trades are anonymized.",
        "tools": [
            {"name": "t_market_flow", "use": "Wallet-level net directional pressure + whale concentration."},
            {"name": "t_whale_trades", "use": "Large-notional trades (filter by min_usd / market)."},
            {"name": "t_leaderboard", "use": "Trader leaderboard (weekly/monthly)."},
            {"name": "t_trader_profile", "use": "One trader's positions + activity + portfolio value."},
            {"name": "t_market_holders", "use": "YES/NO holder breakdown for a market."},
        ],
    },
    {
        "group": "events_and_batch",
        "tools": [
            {"name": "t_event_related_markets", "use": "Markets tied to a firehose event_id (24h window)."},
            {"name": "t_context_batch", "use": "Lean t_market_context digest for up to 25 markets in one call."},
        ],
    },
]

_PRINCIPLES = [
    "Call t_status first to confirm the service is up and the dataset is fresh before trading off it.",
    "market_ref must be venue-prefixed: 'kalshi:<TICKER>' or 'polymarket:<id|slug>' (a full market URL also works). A bare id is rejected with a hint, never a silent null.",
    "Cross-venue equivalence is the core: t_equivalent_markets for one known market, t_matched_pairs to browse, t_find_divergences for the arb radar.",
    "Before treating a cross-venue spread as a lock: check t_market_rules (settlement wording can differ — strict-vs-inclusive thresholds, deadlines) AND t_market_history staleness (a parked/frozen quote makes the edge a ghost).",
    "On the arb radar, sort_by='net_edge' is the honest ranking (fee-netted, executable); sort_by='spread' overstates one-sided books.",
    "Flow / trader / holder tools are Polymarket-only — Kalshi trades are anonymized.",
    "This server is READ-ONLY: no trading keys, no order submission.",
    "Every tool returns a {ok, command, data, meta} envelope — branch on `ok`, read the payload from `data`.",
]

_WORKFLOWS = [
    {
        "goal": "Find and validate a locked cross-venue arbitrage",
        "steps": [
            "t_find_divergences(min_net_edge=0.03, fungible_only=true) — candidates ranked by annualized net edge",
            "t_market_rules(ref) — confirm both legs settle on the SAME condition",
            "t_orderbook(ref) on each leg — confirm executable depth at the quoted price",
            "t_market_history(ref) — confirm neither quote is parked/stale (is_live_event / last_move_age_s)",
        ],
    },
    {
        "goal": "Is this Kalshi market also on Polymarket?",
        "steps": ["t_equivalent_markets('kalshi:<TICKER>') — returns the twin + cross-venue spread"],
    },
    {
        "goal": "Research one market end to end",
        "steps": [
            "t_get_market(ref) — fast core",
            "t_market_context(ref) — news + outcome ladder + siblings",
            "t_market_flow(ref) — Polymarket positioning (who's on each side)",
        ],
    },
    {
        "goal": "What moved today",
        "steps": ["t_screen(sort_by='move', limit=50) — top |24h move| over the high-volume pool"],
    },
    {
        "goal": "Find markets about a news headline",
        "steps": ["t_find_markets('<headline or article text>', exclude_stale=true)"],
    },
]


def agent_guide() -> dict[str, Any]:
    """Return the local self-onboarding playbook (no network)."""
    return {
        "service": "pytheum",
        "summary": (
            "Verified cross-venue prediction-market equivalence + point-in-time "
            "context for Kalshi and Polymarket. The differentiator is the gold set "
            "of settlement-verified same-question pairs (with published precision) "
            "— surfaced as equivalence, divergence/arb, and rules-comparison tools."
        ),
        "guide_version": GUIDE_VERSION,
        "local": True,
        "principles": _PRINCIPLES,
        "conventions": {
            "market_ref": "venue-prefixed: 'kalshi:KXFED-25-MAY' | 'polymarket:558936' | 'polymarket:<slug>' | a market URL",
            "response_envelope": {
                "success": {"ok": True, "command": "<tool>", "data": "<payload>", "meta": {"generated_at": "...", "elapsed_ms": 0, "version": "..."}},
                "error": {"ok": False, "command": "<tool>", "error": "<message>", "data": "<payload or null>", "meta": {"...": "..."}},
                "note": "A venue being down is {ok:true, data:{source:'unavailable'}} — a degraded success, not an error.",
            },
            "pagination": "limit / since / until / full where shown in a tool's help.",
            "read_only": True,
        },
        "tool_groups": _TOOL_GROUPS,
        "workflows": _WORKFLOWS,
    }


def agent_about() -> dict[str, Any]:
    """Return the local "who is Pytheum" brief (no network). Live numbers are
    intentionally NOT hardcoded here: point agents at t_status / t_quality."""
    return {
        "name": "Pytheum",
        "mission": "The information substrate for forecasting and prediction-market agents. The data, not the edge.",
        "what_we_do": "We unify Kalshi, Polymarket, and Manifold into one settlement-verified cross-venue graph, pair every market with real-time world context (news, social, filings, each timestamped), and serve it through a hosted, keyless MCP.",
        "the_wedge": "An agent on Pytheum gets fresher context than classic web search. Search engines lag on indexing and embedding new information flow, so a Pytheum agent always has the current world-state for a market, plus the same question on the other venue and whether a price gap is real money or a stale wall.",
        "vision": "Our end goal is full point-in-time market replay: the world exactly as it was before a market resolved, contamination-filtered with no look-ahead. This is the roadmap, not a current capability.",
        "what_we_are_not": "Not a signal product. We do not sell edges, place orders, or hold funds. Read-only. The model, sizing, and execution are yours.",
        "data": "Live coverage and freshness via t_status. Graph integrity via t_quality.",
        "tools": "About 25 t_* tools for discovery, real-time context, cross-venue divergences net of fees, prices and history. Call t_guide to start.",
        "pricing": "Free to use.",
        "founders": [
            {"name": "Ali Bauyrzhan", "role": "Founder", "bio": "Summer SDE Intern at Amazon Ads, previously research at Columbia Business School.", "linkedin": "https://www.linkedin.com/in/alibaur/", "contact": "ab5867@columbia.edu"},
            {"name": "Konstantinos Anagnostopoulos", "role": "Cofounder", "bio": "SDE at Tavily (acquired by Nebius), previously TA'd a graduate-level databases course as an undergraduate.", "linkedin": "https://www.linkedin.com/in/kon-anagn/", "contact": "ka3037@columbia.edu"},
        ],
        "links": {"site": "https://pytheum.com", "api": "https://api.pytheum.com", "mcp": "https://api.pytheum.com/mcp", "repo": "https://github.com/pytheum/pytheum"},
    }


def guide_tool_names() -> set[str]:
    """Every tool name referenced in the playbook — asserted against the live registry."""
    return {t["name"] for grp in _TOOL_GROUPS for t in grp["tools"]}
