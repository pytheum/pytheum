# Pytheum Python SDK — design (`pytheum.client`)

**Goal:** a production-grade, typed Python client over the pytheum REST API so a
customer gets the 27 capabilities as a library, not hand-rolled `httpx`. Concurrency-,
throughput-, and latency-optimized; stress-tested against live prod.

## Where
`src/pytheum/client/` in the `pytheum` repo. Public API:
```python
from pytheum.client import Client, AsyncClient, PytheumError, RateLimitError
```
Deps: **httpx only** (+ stdlib). No server deps — importing the client never pulls the
MCP/API server. `AsyncClient` is primary (throughput); `Client` is a true sync client
(httpx.Client, not asyncio-wrapped).

## Concurrency / throughput / latency model (the load-bearing part)
- **Single reused client** per instance → connection pooling (httpx `Limits`:
  `max_connections`, `max_keepalive_connections`). Never a client-per-request.
- **Granular timeouts**: connect / read / write / pool, all tunable.
- **Client-side concurrency governor**: an `asyncio.Semaphore` (async) /
  `threading.BoundedSemaphore` (sync) bounding in-flight requests (default **8**). The
  live stress ramp showed ~5–10 concurrent/endpoint/IP is the safe envelope before the
  edge's per-IP limiter kicks; the governor keeps a customer from self-429ing while
  maximizing throughput under the cap.
- **Retry policy** (bounded, exponential backoff + full jitter):
  - Retry on: `ConnectError`, `ConnectTimeout`, `ReadTimeout`, `PoolTimeout`, and
    status **429 / 502 / 503 / 504**.
  - **429 honors `Retry-After`** (seconds or HTTP-date) exactly; else backoff.
  - Never retry other 4xx. `max_retries` default 3; backoff base 0.25s, cap 8s.
- **Batch helpers**: `gather(*awaitables)` + typed `*_many` fan-out helpers run under the
  governor — the throughput story (N refs hydrated concurrently, safely).

## Errors (taxonomy)
`PytheumError` (base) → `APIError(status, body, hint)` → `RateLimitError(429)`,
`NotFoundError(404)`, `AuthError(401/403)`, `ServerError(5xx)`, `PytheumTimeout`,
`ConnectionFailed`. The API's `{error, hint}` body is surfaced on the exception.

## Response shapes (REST returns DIRECT payloads — no `{ok,data}` envelope; that's MCP)
- `/v1/status` → `{equivalence, related, hl_related, service, platforms}`
- `/v1/markets/matched` → `{pairs:[{kalshi, polymarket, bet_type, confidence, method, cross_venue, is_live}], total, meta}`
- `/v1/markets/search` → `{markets:[{id, question, venue, bundle_id, status, volume_usd, liquidity_usd, url, resolution_at, days_to_resolution, implied_yes, book, resolution}], count, meta}`
- (models mirror these; `from_dict` is lenient — unknown keys ignored, `.raw` preserved.)

## Endpoint registry (drives methods + models) — all 21 REST routes
| method | http | path | key params |
|---|---|---|---|
| `status` | GET | /v1/status | — |
| `quality` | GET | /v1/quality | — |
| `about` | GET | /v1/about | — |
| `guide` | GET | /v1/guide | — |
| `search` | GET | /v1/markets/search | q, limit, venue, min_similarity |
| `screen` | GET | /v1/markets/screen | venue, status, min_volume, max_volume, sort_by, limit |
| `get_market` | GET | /v1/markets/{ref}/core | ref |
| `equivalents` | GET | /v1/markets/{ref}/equivalents | ref, limit, include_rules |
| `matched_pairs` | GET | /v1/markets/matched | bet_type, q, min_volume, sort_by, limit, offset |
| `related` | GET | /v1/markets/{ref}/related | ref, limit |
| `rules` | GET | /v1/markets/{ref}/rules | ref |
| `context` | GET | /v1/markets/{ref}/context | ref, limit |
| `bundle_context` | GET | /v1/bundles/{ref}/context | ref, limit |
| `context_batch` | GET | /v1/markets/context-batch | refs (csv) |
| `event_related_markets` | GET | /v1/events/{id}/related-markets | id, limit |
| `orderbook` | GET | /v1/markets/{ref}/book | ref |
| `trades` | GET | /v1/markets/{ref}/trades | ref |
| `ohlcv` | GET | /v1/markets/{ref}/ohlcv | ref, interval, limit |
| `open_interest` | GET | /v1/markets/{ref}/oi | ref |
| `history` | GET | /v1/markets/{ref}/history | ref |
| `flow` | GET | /v1/markets/{ref}/flow | ref |
| `leaderboard` | GET | /v1/traders/leaderboard | period |
| `trader` | GET | /v1/traders/{wallet} | wallet |
| `holders` | GET | /v1/markets/{ref}/holders | ref |
| `whale_trades` | GET | /v1/markets/whale-trades | min_usd, limit |

`find_divergences(...)` = convenience over `matched_pairs(sort_by="net_edge")` (the arb
radar); no separate route. `{ref}` is `venue:id` (url-encoded).

## Testing
- **Unit** (`httpx.MockTransport`): retry counts, 429+Retry-After honored, backoff bounded,
  governor caps in-flight, error mapping, param/path building, envelope-less parsing.
- **Real integration** (gated `PYTHEUM_SDK_LIVE=1`): every method against live prod → sane.
- **Stress/throughput** (gated): fan out N concurrent calls through the governor vs live prod,
  measure throughput + p50/p95, assert the governor keeps 429s → transparent retries (no
  surfaced failures) and stays inside the safe envelope. Bounded (respect the 16GB box).
