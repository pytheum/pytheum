"""GET /llms.txt — agent-readable plain-text manifest of the Pytheum API.

Keyless, public. Enumerates every /v1 REST route with one-line descriptions
so an LLM agent can discover capabilities without reading full documentation.

Sync contract
-------------
The test in tests/test_llms_txt.py asserts every /v1 path registered in
server.py also appears in ENDPOINT_PATHS (checked against LLMS_TXT text).
When you add a new route in server.py._bind_api_router or ._bind_trader_routes,
add it here too.
"""
from __future__ import annotations

_BASE_URL = "https://api.pytheum.com"
_MCP_URL = "https://api.pytheum.com/mcp"

_PROVENANCE = (
    "Data provenance: cross-venue pairs carry settlement-verified equivalence "
    "(Kalshi<->Polymarket). Verification method and match confidence disclosed "
    "per-pair via the `method` and `confidence` fields. Sources disclosed per "
    "response; live prices fetched in real time from venue APIs."
)

# Ordered list of (route, one-line description).  Route uses the same
# {param} placeholder convention as api/routes.py.  This is the source
# of truth — the test asserts every /v1 path registered in server.py
# appears in ENDPOINT_PATHS below.
_ENDPOINT_ROWS: tuple[tuple[str, str], ...] = (
    ("GET /v1/status",
     "Service health check and dataset summary — keyless, no auth required."),
    ("GET /v1/markets/screen",
     "Structured market screen: filter by venue, status, volume, liquidity, resolution window."),
    ("GET /v1/markets/equivalents",
     "Collection of verified Kalshi<->Polymarket pairs joined to live"
     " quotes (used by t_find_divergences)."),
    ("GET /v1/markets/matched",
     "Paginated, filterable view of all 136k settlement-verified"
     " cross-venue pairs with live prices."),
    ("GET /v1/markets/context-batch",
     "Batch news/social/macro context for up to 25 markets in one call."),
    ("GET /v1/markets/relevant-to",
     "Semantic search: prediction markets related to a free-form text query."),
    ("GET /v1/markets/whale-trades",
     "Recent large-notional Polymarket trades above a USD threshold."),
    ("GET /v1/markets/{ref}/context",
     "News, social, and macro events paired with a specific market."),
    ("GET /v1/markets/{ref}/equivalents",
     "Settlement-verified counterpart market on the other venue with live spread."),
    ("GET /v1/markets/{ref}/rules",
     "Full resolution rules text for a market and its cross-venue equivalent, side by side."),
    ("GET /v1/markets/{ref}/related",
     "Correlated (non-equivalent) cross-venue markets with basis notes."),
    ("GET /v1/markets/{ref}/history",
     "Point-in-time price and book history with staleness flags and derived moves."),
    ("GET /v1/markets/{ref}/ohlcv",
     "OHLCV candles — pytheum capture first, venue candles as fallback."),
    ("GET /v1/markets/{ref}/flow",
     "Wallet-level trade flow for a Polymarket market"
     " (directional pressure + whale concentration)."),
    ("GET /v1/markets/{ref}/book",
     "Live orderbook snapshot with top-of-book summary."),
    ("GET /v1/markets/{ref}/trades",
     "Recent trade tape for a market (live venue fetch, cached ~10 s)."),
    ("GET /v1/markets/{ref}/oi",
     "Current open interest for a market (live venue fetch, cached ~30 s)."),
    ("GET /v1/markets/{ref}/holders",
     "Token holder breakdown for a Polymarket market."),
    ("GET /v1/bundles/{ref}/context",
     "Events paired with all markets in a bundle, deduplicated by event_id."),
    ("GET /v1/events/{event_id}/related-markets",
     "Prediction markets related to a specific firehose event_id."),
    ("GET /v1/traders/leaderboard",
     "Polymarket trader leaderboard ranked by profit (weekly or monthly)."),
    ("GET /v1/traders/{wallet}",
     "Polymarket trader profile: positions, recent activity, and portfolio value."),
    ("GET /v1/stream/metrics",
     "Prometheus-format server metrics (connections, fanned-out event counts)."),
    ("WSS /v1/stream",
     "WebSocket firehose: tick_price, tick_book, news_headline,"
     " social_post, hn_story, macro_release."),
)

# Set of path strings used by the test to assert route coverage.
# Excludes WSS entry (not a REST route) and /v1/stream/metrics (not in api_router).
ENDPOINT_PATHS: frozenset[str] = frozenset(
    row[0].split(" ", 1)[1]  # strip "GET " prefix → path only
    for row in _ENDPOINT_ROWS
    if row[0].startswith("GET /v1/")
)


def _build() -> str:
    lines: list[str] = [
        "# Pytheum API — agent manifest",
        "",
        "Pytheum is a real-time prediction market intelligence API providing",
        "verified cross-venue equivalence data (Kalshi<->Polymarket), live order-",
        "book quotes, news/social context, and trader analytics.",
        "",
        f"Base URL:      {_BASE_URL}",
        f"MCP connector: {_MCP_URL}",
        "",
        _PROVENANCE,
        "",
        "## REST endpoints",
        "",
    ]
    for route, desc in _ENDPOINT_ROWS:
        lines.append(f"  {route}")
        lines.append(f"    {desc}")
        lines.append("")
    return "\n".join(lines)


# Pre-built constant — computed once at import time, served on every request.
LLMS_TXT: str = _build()
