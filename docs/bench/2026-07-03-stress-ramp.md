# Stress ramp — customer-outreach readiness (2026-07-03)

- **Target**: `https://api.pytheum.com` (fronted by Caddy, documented per-IP limit 100 req/s, 16GB box)
- **MCP target**: `https://mcp.pytheum.com/`
- **Run started**: 2026-07-03 04:27:37 UTC
- **Total runtime**: 7.9 min (475s)
- **Script**: `scripts/stress_ramp.py`
- **Method**: gradual, self-limiting concurrency ramp. Per endpoint, 30s at each concurrency level `[5, 10, 20, 40]` (N worker coroutines: request -> 50ms think time -> repeat), 3s between levels, 10s between endpoints. **Hard rule**: any level with >20% non-200 responses aborts the ramp for that endpoint (no higher levels attempted). A separate whole-run abort rule fires on broad multi-endpoint 5xx distress.

## Phase 1 — REST ramp results

| Endpoint | Concurrency | req/s | ok(200) | 429 | 5xx | other | error% | p50 | p95 | p99 | max |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| `/v1/status` | 5 | 47.7 | 1431 | 0 | 0 | 0 | 0.0% | 31ms | 125ms | 170ms | 216ms |
| `/v1/status` | 10 **HARD-STOP** | 112.3 | 2688 | 681 | 0 | 0 | 20.2% | 25ms | 116ms | 241ms | 380ms |
| `/v1/markets/search` | 5 | 56.8 | 1703 | 0 | 0 | 0 | 0.0% | 28ms | 68ms | 113ms | 1303ms |
| `/v1/markets/search` | 10 **HARD-STOP** | 126.7 | 2840 | 962 | 0 | 0 | 25.3% | 25ms | 35ms | 84ms | 814ms |
| `/v1/markets/screen` | 5 | 56.5 | 1695 | 0 | 0 | 0 | 0.0% | 27ms | 53ms | 90ms | 2097ms |
| `/v1/markets/screen` | 10 **HARD-STOP** | 122.9 | 2842 | 845 | 0 | 0 | 22.9% | 27ms | 35ms | 52ms | 1354ms |
| `/v1/markets/matched` | 5 | 30.1 | 904 | 0 | 0 | 0 | 0.0% | 105ms | 155ms | 349ms | 643ms |
| `/v1/markets/matched` | 10 | 30.3 | 908 | 0 | 0 | 0 | 0.0% | 271ms | 324ms | 744ms | 3259ms |
| `/v1/markets/matched` | 20 | 25.9 | 777 | 0 | 0 | 0 | 0.0% | 686ms | 1020ms | 2703ms | 3354ms |
| `/v1/markets/matched` | 40 | 22.1 | 662 | 0 | 0 | 0 | 0.0% | 1550ms | 4739ms | 9286ms | 13047ms |
| `/v1/markets/equivalents` | 5 | 58.8 | 1765 | 0 | 0 | 0 | 0.0% | 29ms | 57ms | 114ms | 346ms |
| `/v1/markets/equivalents` | 10 | 118.3 | 2926 | 622 | 0 | 0 | 17.5% | 32ms | 61ms | 105ms | 238ms |
| `/v1/markets/equivalents` | 20 **HARD-STOP** | 242.4 | 2987 | 4285 | 0 | 0 | 58.9% | 35ms | 84ms | 143ms | 196ms |

### Hard-stops

- `/v1/status`: 10 concurrency -> 20% non-200 (429=681, 5xx=0, other=0) — exceeded 20% hard-stop threshold, aborting ramp for this endpoint
- `/v1/markets/search`: 10 concurrency -> 25% non-200 (429=962, 5xx=0, other=0) — exceeded 20% hard-stop threshold, aborting ramp for this endpoint
- `/v1/markets/screen`: 10 concurrency -> 23% non-200 (429=845, 5xx=0, other=0) — exceeded 20% hard-stop threshold, aborting ramp for this endpoint
- `/v1/markets/equivalents`: 20 concurrency -> 59% non-200 (429=4285, 5xx=0, other=0) — exceeded 20% hard-stop threshold, aborting ramp for this endpoint

### Max SAFE concurrency per endpoint

Defined as: the last tested level with <1% errors AND p99 < 2000ms. If no level meets both, that's stated explicitly.

- `/v1/status`: **5** (p99=170ms, error=0.00%) (ramp hard-stopped at concurrency=10)
- `/v1/markets/search`: **5** (p99=113ms, error=0.00%) (ramp hard-stopped at concurrency=10)
- `/v1/markets/screen`: **5** (p99=90ms, error=0.00%) (ramp hard-stopped at concurrency=10)
- `/v1/markets/matched`: **10** (p99=744ms, error=0.00%)
- `/v1/markets/equivalents`: **5** (p99=114ms, error=0.00%) (ramp hard-stopped at concurrency=20)

## Phase 2 — limiter provocation (`/v1/status`, 150-request unpaced burst)

- Total: 150, 200: 26, 429: 124, 5xx: 0, other: 0
- **Limiter verdict**: PASS (clean 429s observed)
- Sample 429 `Retry-After` header: `1`
- Sample 429 headers: `{"alt-svc": "h3=\":443\"; ma=2592000", "retry-after": "1", "server": "Caddy", "date": "Fri, 03 Jul 2026 04:35:15 GMT", "content-length": "0"}`
- Sample 429 body: ``
- **Recovery**: 200s resumed **0.30s** after burst end.

## Phase 3 — MCP cold-spike re-verify

Old baseline (2026-07-03, pre-warmup-fix): `t_screen` cold **7.77s** / `t_find_divergences` cold **2.69s**.

| Tool | Cold (this run) | Warm p95 (10 calls) | Notes |
|---|---:|---:|---|
| `t_screen` | 12798ms | 96ms |  |
| `t_find_divergences` | 1104ms | 197ms |  |

- `t_screen`: old cold baseline 7.77s -> measured this run **12.80s** (does NOT clearly confirm the warmup fix).
- `t_find_divergences`: old cold baseline 2.69s -> measured this run **1.10s** (CONFIRMS warmup fix).

## Phase 4 — 5xx window scan

No 5xx responses observed anywhere in the run.

## Delivery-eve verdict

The API held up cleanly under a real, gradual ramp — **zero 5xx errors anywhere in the
entire ~8-minute run**, and every non-200 response was a clean, well-formed `429` from
Caddy's edge limiter with a `Retry-After` header, recovering to 200s in well under a
second after the deliberate 150-request burst. For everyday customer traffic, **5
concurrent clients per endpoint is comfortably safe** for `/v1/status`,
`/v1/markets/search`, `/v1/markets/screen`, and `/v1/markets/equivalents` (all clean at
concurrency=5, all hit the ~100 req/s edge limiter and started drawing 429s at
concurrency=10-20 — expected, correct backpressure, not breakage). `/v1/markets/matched`
never triggered the rate limiter at all, but its latency degrades hard under load — p99
went from 349ms at concurrency=5 to 9.3s (max 13s) at concurrency=40 — so treat its safe
envelope as **10 concurrent**, gated by latency rather than errors; a customer hammering
this one specific endpoint harder than that will get slow, not clearly-rejected,
responses. One real anomaly worth flagging before tomorrow: **the MCP `t_screen` cold
call measured 12.8s** (reproduced at 12.5s in an earlier smoke pass), which is *worse*
than the pre-fix baseline of 7.77s and far from the ~100-300ms the recent warmup fix was
expected to deliver — `t_find_divergences` cold did improve (1.1s vs 2.69s baseline,
though still above the ~100-300ms target). The likely explanation: `warmup_ping.py`'s
4-minute cron only warms the **REST** paths (`/v1/markets/screen`,
`/v1/markets/equivalents`) to keep Postgres buffers hot — it never makes an actual MCP
`tools/call`, so any MCP-session-layer cold-start cost (distinct from DB buffer warmth)
is untouched by the fix. Worth a follow-up before assuming the MCP surface is warm for a
customer's literal first tool call tomorrow. Net: safe to point customers at this API
tomorrow within the concurrency levels above; the rate limiter and REST surface behaved
exactly as designed, and the one residual risk (`t_screen` cold-start) is a latency
inconvenience on a single MCP tool's first call, not a correctness or availability
problem.
