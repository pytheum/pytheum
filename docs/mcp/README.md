# pytheum MCP Server

The pytheum Model Context Protocol (MCP) server gives an AI agent direct,
read-only access to the **verified prediction-market graph**: settlement-verified
Kalshi↔Polymarket equivalence pairs, live orderbook quotes, point-in-time price
history, news/social/macro context, and Polymarket trader analytics — all behind
27 tools an agent can call without scraping two venues by hand.

- **Read-only.** No tool places, modifies, or cancels an order. There are no
  trading keys anywhere in the server. Every tool carries `readOnlyHint: true`.
- **Source-disclosed.** Live numbers are fetched from venue APIs in real time and
  every response names its `source` (`live` / `pit_archive` / `venue_live` /
  `unavailable`). Cross-venue pairs carry a per-pair `method` + `confidence` so a
  match is always auditable.
- **Two connection modes.** A hosted always-on connector (zero install — serves
  the full current graph) and a local self-hosted server that runs offline
  against dataset export files **you provide** (no keys, no network; the package
  itself ships no data).

| Doc | Contents |
|---|---|
| **[tools.md](tools.md)** | Complete per-tool reference: params, return shapes, annotations, example call + response, offline-vs-hosted, "use when" guidance. |
| **[compliance.md](compliance.md)** | Rate limits, data freshness / point-in-time disclosure, "substrate not signal" framing, licensing, registry-listing checklist. |
| **[server.json](server.json)** | MCP Registry metadata template (reverse-DNS name, remotes, packages). |

---

## What the tools cover

| Group | Tools | Mode |
|---|---|---|
| **Meta & onboarding** | `t_guide`, `t_about`, `t_status`, `t_quality` | Local / offline-capable* |
| **Cross-venue equivalence** | `t_equivalent_markets`, `t_matched_pairs`, `t_market_rules`, `t_related_markets`, `t_find_divergences` | Offline-capable* |
| **Discovery & context** | `t_find_markets`, `t_search_markets`, `t_screen`, `t_get_market`, `t_market_context`, `t_bundle_context`, `t_context_batch`, `t_event_related_markets` | Hosted |
| **Live market data** | `t_orderbook`, `t_recent_trades`, `t_open_interest`, `t_ohlcv`, `t_market_history`, `t_market_flow` | Hosted |
| **Trader analytics (Polymarket-only)** | `t_leaderboard`, `t_trader_profile`, `t_market_holders`, `t_whale_trades` | Hosted |

\* Offline means `pytheum serve` with `PYTHEUM_EQUIVALENCE_PATH` /
`PYTHEUM_RELATED_PATH` pointing at dataset export files — the exports are **not
bundled** in the wheel or this repo (only their checksums are pinned in
`datasets/MANIFEST.json`); without them the dataset tools return empty results
(with a `file_missing` flag), never errors. `t_guide` / `t_about` are fully
local (no data, no network). Live prices and edges that offline-capable tools
normally splice in are absent offline (fields present but `null`);
`t_find_divergences` returns its verified pairs but cannot compute a live locked
edge without a hosted book join. See each tool's **Mode** line in
[tools.md](tools.md).

---

## Connection mode 1 — Hosted connector (zero install)

The simplest path. Point any MCP client at the always-on streamable-HTTP endpoint:

```
https://api.pytheum.com/mcp
```

No API key, no local process. The endpoint is rate-limited per IP (see
[compliance.md](compliance.md)).

### Claude Desktop / Claude.ai

Settings → Connectors → Add custom connector → paste `https://api.pytheum.com/mcp`.

### Cursor

`~/.cursor/mcp.json` (or **Settings → MCP → Add**):

```json
{
  "mcpServers": {
    "pytheum": {
      "url": "https://api.pytheum.com/mcp"
    }
  }
}
```

### Windsurf

`~/.codeium/windsurf/mcp_config.json`:

```json
{
  "mcpServers": {
    "pytheum": {
      "serverUrl": "https://api.pytheum.com/mcp"
    }
  }
}
```

### Claude Code (CLI)

```bash
claude mcp add --transport http pytheum https://api.pytheum.com/mcp
```

---

## Connection mode 2 — Local self-hosted server

Run the server yourself. No secrets, no network for the offline routes — but
note the package **ships no data**: the dataset tools serve real pairs only if
you point them at equivalence/related export files (the hosted endpoint above
serves the full current graph with zero setup).

```bash
pip install pytheum

# Optional — self-host with data (otherwise dataset tools return empty results):
export PYTHEUM_EQUIVALENCE_PATH=/path/to/equivalence-export.jsonl.gz
export PYTHEUM_RELATED_PATH=/path/to/related-export.jsonl.gz

pytheum serve --mcp        # HTTP API on :8080, MCP on :8444
```

The banner prints which routes are live vs degraded. Point your client at the
local URL:

```json
{
  "mcpServers": {
    "pytheum": {
      "url": "http://127.0.0.1:8444/mcp"
    }
  }
}
```

Offline, the cross-venue tools (`t_status`, `t_quality`, `t_equivalent_markets`,
`t_matched_pairs`, `t_market_rules`, `t_related_markets`) serve from your local
dataset files (empty results with a `file_missing` flag if none are configured).
The discovery, live-data, and trader tools require the hosted API (they hit
venue endpoints and the embeddings/PIT store), and degrade to a
`source: "unavailable"` / empty result rather than erroring when run offline.

To use the hosted API for those tools while still running the MCP locally, set:

```bash
PYTHEUM_API_BASE=https://api.pytheum.com pytheum serve --mcp
```

---

## Data provenance (the honest version)

- **Cross-venue pairs are settlement-verified** — matched on event identity and
  resolution semantics, not fuzzy title similarity. Each pair discloses the
  `method` that decided it (`structured_key`, `game_title_match`,
  `human_adjudicated`, `opus_backstop`, …) and a `confidence` (deterministic
  structural methods carry `null` confidence by design — they are exact, not
  scored). Filter to deterministic-only pairs with `fungible_only: true`.
- **Live prices are real-time venue fetches**, cached server-side for 2–300 s
  depending on the tool (disclosed per tool). They are not a consolidated feed —
  a quote can be a frozen *parked wall* (a resting limit order behind a tight
  spread); tools surface `is_parked_wall` / `last_move_age_s` so an agent never
  treats a stale quote as a live price.
- **Point-in-time history is pytheum's own capture** (`t_market_history`,
  `t_ohlcv`) — no lookahead, backtest-grade — with venue candles as a disclosed
  fallback.
- **Trader analytics are Polymarket-only.** Kalshi trades are anonymized, so no
  holder / leaderboard / wallet-flow equivalent exists on that venue; those tools
  say so explicitly rather than returning empty.

See [compliance.md](compliance.md) for freshness windows, rate limits, and the
"not financial advice" framing.
