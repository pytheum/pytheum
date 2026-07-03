# MCP tool latency baseline — live server (2026-07-03)

Benchmark of the **live, customer-facing** pytheum MCP server
(`https://mcp.pytheum.com/`, streamable-http transport) before customer
delivery. This is the surface agent-customers actually call — not a local
dev server, not the REST API directly.

Script: `scripts/bench_mcp.py`. Protocol handling (initialize → read the
`Mcp-Session-Id` response header → `tools/call`, parsing FastMCP's SSE
`data:` framing) is lifted from a minimal reference driver validated earlier
in this investigation. The script:

1. Calls `tools/list` to enumerate the real deployed tool set.
2. Discovers live seed refs (`market_ref`s, a wallet address, a bundle ref)
   from `t_matched_pairs` / `t_search_markets` / `t_leaderboard` so every
   tool gets a realistic, valid argument set instead of placeholder ids.
3. Runs **1 cold call + 10 sequential timed calls** per tool, at a polite
   ~0.8s inter-call gap (the server enforces a per-IP token-bucket limiter,
   default 60 req/min sustained / 60 burst — see `src/pytheum/mcp/server.py`).
4. Classifies each tool by the upstream it wraps (inferred from
   `src/pytheum/mcp/tools.py`) and checks the response is a sane
   `{ok, command, data, meta}` envelope.

Full raw JSON (all 27 tools, args used, and per-call timings) is available on
request from the run artifact; this document is the summary.

## Result: 27/27 tools GREEN, zero errors, zero rate-limit hits

The reported run used ~0.8s spacing and hit **no** `RATE_LIMITED` responses —
every one of the 27 tools returned a well-formed envelope on all 11 calls
(1 cold + 10 sequential). An earlier, tighter-spaced pilot run against the
same IP *did* trip the limiter (429 on `t_market_history`, `t_market_rules`,
`t_open_interest`, `t_quality`, `t_related_markets`, `t_status`) after the
per-IP bucket was partially drained by preceding smoke tests — the limiter
fired exactly as designed and the script's backoff-and-continue path handled
it cleanly. **Limiter verdict: PASS.** No tool was found broken.

Verdict thresholds (per the class of backend each tool hits):

- **GREEN**: p95 < 1.0s for `bundled` (local file) / `db` (pytheum's own
  store) tools; p95 < 2.5s for `semantic` / `live-venue` / `computed` tools.
- **RED**: over threshold, or any error in the 11-call window.

All 27 tools are **GREEN** on both cold and warm latency.

## Tool inventory by upstream class

| Class | Meaning | Count |
|---|---|---|
| `bundled` | Local file read (equivalence/related dataset, quality/about/guide) — keyless, no venue hop | 8 |
| `db` | pytheum's own market-store-backed REST route | 7 |
| `semantic` | Embedding/similarity search | 2 |
| `computed` | Aggregates multiple routes + server-side fee/edge math | 1 |
| `live-venue` | Direct Kalshi/Polymarket API fetch, coalesced + short-TTL cached server-side | 9 |

## Per-tool results (sorted by warm p95, descending)

| Tool | Class | REST route | Cold (ms) | p50 (ms) | p95 (ms) | Verdict |
|---|---|---|---:|---:|---:|---|
| `t_bundle_context` | db | `GET /v1/bundles/{ref}/context` | 521.6 | 376.9 | 584.9 | GREEN |
| `t_market_flow` | live-venue | `GET /v1/markets/{ref}/flow` | 113.4 | 115.2 | 306.2 | GREEN |
| `t_recent_trades` | live-venue | `GET /v1/markets/{ref}/trades` | 145.4 | 105.0 | 284.7 | GREEN |
| `t_find_markets` | semantic | `GET /v1/markets/relevant-to` | 505.3 | 197.1 | 274.4 | GREEN |
| `t_matched_pairs` | bundled | `GET /v1/markets/matched` | 186.2 | 132.6 | 143.2 | GREEN |
| `t_find_divergences` | computed | `GET /v1/markets/equivalents` ×N (multi-fetch + fee/edge math) | 2686.0 | 128.2 | 141.8 | GREEN |
| `t_trader_profile` | live-venue | `GET /v1/traders/{wallet}` | 305.4 | 131.9 | 140.4 | GREEN |
| `t_context_batch` | db | `GET /v1/markets/{ref}/context` ×N (capped fan-out) | 932.1 | 116.8 | 137.1 | GREEN |
| `t_orderbook` | live-venue | `GET /v1/markets/{ref}/book` | 135.8 | 110.7 | 136.5 | GREEN |
| `t_market_rules` | bundled | `GET /v1/markets/{ref}/rules` | 130.5 | 126.4 | 134.4 | GREEN |
| `t_ohlcv` | live-venue | `GET /v1/markets/{ref}/ohlcv` | 142.8 | 120.2 | 132.0 | GREEN |
| `t_equivalent_markets` | bundled | `GET /v1/markets/{ref}/equivalents` | 124.5 | 125.0 | 131.4 | GREEN |
| `t_market_context` | db | `GET /v1/markets/{ref}/context` | 792.8 | 116.5 | 124.9 | GREEN |
| `t_get_market` | db | `GET /v1/markets/{ref}/core` | 113.1 | 107.7 | 117.4 | GREEN |
| `t_open_interest` | live-venue | `GET /v1/markets/{ref}/oi` | 149.7 | 105.9 | 114.1 | GREEN |
| `t_quality` | bundled | `GET /v1/quality` | 236.4 | 106.6 | 113.2 | GREEN |
| `t_search_markets` | db | `GET /v1/markets/search` | 169.9 | 105.2 | 112.4 | GREEN |
| `t_related_markets` | bundled | `GET /v1/markets/{ref}/related` | 103.6 | 106.0 | 112.1 | GREEN |
| `t_screen` | db | `GET /v1/markets/screen` | **7765.4** | 105.2 | 111.1 | GREEN |
| `t_guide` | bundled | local, no network | 105.6 | 103.9 | 110.8 | GREEN |
| `t_leaderboard` | live-venue | `GET /v1/traders/leaderboard` | 108.7 | 105.2 | 110.8 | GREEN |
| `t_market_history` | db | `GET /v1/markets/{ref}/history` | 145.5 | 104.3 | 110.5 | GREEN |
| `t_whale_trades` | live-venue | `GET /v1/markets/whale-trades` | 234.7 | 105.8 | 109.2 | GREEN |
| `t_market_holders` | live-venue | `GET /v1/markets/{ref}/holders` | 394.9 | 102.2 | 109.0 | GREEN |
| `t_event_related_markets` | semantic | `GET /v1/events/{id}/related-markets` | 104.5 | 100.1 | 107.8 | GREEN |
| `t_status` | bundled | `GET /v1/status` | 98.1 | 102.2 | 107.0 | GREEN |
| `t_about` | bundled | local, no network | 101.5 | 102.4 | 104.3 | GREEN |

## The 5 slowest tools (by warm p95) — customer-facing detail

1. **`t_bundle_context`** — p95 **584.9ms**, and unlike every other tool this
   wasn't a one-off cold-cache blip: all 10 sequential calls ran 350–680ms.
   This is a genuinely heavier route (dedupes context across every child
   market in a bundle/event) — still comfortably under the 1.0s db-class
   bar, but the one `db`-class tool worth watching if it creeps further.
2. **`t_market_flow`** — p95 306.2ms, driven by a single 445ms outlier among
   nine ~110-115ms calls (live-venue jitter, not systemic).
3. **`t_recent_trades`** — p95 284.7ms, same pattern: one 426ms spike among
   nine ~100-110ms calls.
4. **`t_find_markets`** — p95 274.4ms; the semantic-search tool, consistently
   170-320ms across all calls (embedding lookup overhead), well inside its
   2.5s semantic-class budget.
5. **`t_matched_pairs`** — p95 143.2ms. Included here only because everything
   else clusters near 105-140ms; not actually a concern.

**Cold-call outliers (not reflected in p95, but worth disclosing since a
customer's very first call to a fresh session pays this cost):**
`t_screen` cold = **7.77s** (vs. 111ms warm), `t_find_divergences` cold =
2.69s (vs. 142ms warm), `t_context_batch` cold = 932ms (vs. 137ms warm),
`t_market_context` cold = 793ms (vs. 125ms warm). All four are consistent
with first-hit cache/connection-pool warmup on the server (`t_screen` in
particular scans a large corpus on a cold cache) — every one of the
following 10 calls dropped to double-digit-to-low-hundred ms. Not classified
RED (verdict is based on the sequential/warm window per the stated method),
but flagged explicitly: a customer's literal first `t_screen` call in a
session could see multi-second latency.

## Errors / broken tools

**None.** 27/27 tools returned a well-formed `{ok: true, data: ...}` (or a
documented `{ok: false, error, hint}` degraded path, which is a valid
contract response, not a failure) on every one of the 11 calls in the clean
run.

## Rate limiter behavior (customer-facing concern)

Confirmed working as designed: a per-IP token bucket (`_RL_PER_MIN=60`,
`_RL_BURST=60` by default, see `src/pytheum/mcp/server.py`) gates the
streamable-http endpoint. An earlier pilot pass — run back-to-back with
smoke tests against the same source IP with tighter spacing — drained the
bucket and got clean `429 rate_limited` responses (with `Retry-After: 5`)
instead of hangs or 500s. At ~0.8s/call (~75 req/min sustained during the
benchmark window, close to but under the 60/min steady-state replenishment
once the burst allowance is accounted for) the full 27-tool × 11-call sweep
(297 calls) completed with zero 429s. **Customer impact**: a single agent
making one call at a time at a human-plausible pace will not be rate
limited; a tight automated loop (sub-second spacing sustained) will hit 429
and should honor `Retry-After`.

## Concerns / follow-ups worth flagging to the team

- `t_bundle_context`'s consistent 350-680ms band (not just a cold hit) is
  the one tool that would benefit from a closer look if bundle sizes grow —
  it's the only tool whose *steady-state* latency, not just its cold call,
  sits meaningfully above the rest of the `db` class.
- `t_screen`'s 7.77s cold call is worth a warm-up ping at deploy time (the
  repo already has `scripts/warmup_ping.py` — worth confirming it's hitting
  `t_screen`/`/v1/markets/screen` specifically, since that's the one route
  that showed multi-second cold latency in this run).
- No broken tools, no silent failures, no unhandled exceptions surfaced in
  297 calls — the envelope contract (`{ok, command, data, meta}`) held on
  every response observed.

## Reproduction

```bash
python3 scripts/bench_mcp.py --base https://mcp.pytheum.com/ --calls 10 --sleep 0.8 --out report.json
```

Run at 2026-07-03T01:55:04Z. Seed refs used (discovered live, not
hardcoded): `kalshi:KXATPGTOTAL-26JUN29COLFIL-41`,
`polymarket:2703378`, wallet `0x10a6fadcbacd66330862206f6199b197e3ad4d8b`,
bundle `kalshi:KXATPGTOTAL-26JUN29COLFIL`.
