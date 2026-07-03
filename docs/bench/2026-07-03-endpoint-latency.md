# Live endpoint latency baseline

- **Target**: `https://api.pytheum.com`
- **Run started**: 2026-07-03 01:51:42 UTC
- **Total runtime**: 77.7s
- **Method**: per endpoint — 1 cold call, then N=15 sequential calls, then a 10-concurrent burst of 20 calls. Endpoint groups spaced 3s apart. Keyless per-IP rate limit is 120 req/min — bursts are EXPECTED to trigger 429s; a clean 429 (not a 5xx/hang) is a PASS for limiter behavior.
- **GREEN thresholds**: p95 < 300ms for DB/file-backed & computed endpoints, p95 < 1500ms for live-venue-proxy endpoints. AMBER = within 2x threshold. RED = beyond that, any non-200/429 status (routing gap or 5xx) on any call, a payload-sanity failure, or zero successful (200) calls.
- **Earlier baselines** (context): semantic search sub-second; substring search ~72ms median post-pg_trgm; `/v1/status` SWR ~instant after the first (cold) call.

| Endpoint | Class | Cold | p50 | p95 | p99 | max | Statuses | 429s | Payload OK | Verdict |
|---|---|---|---|---|---|---|---|---|---|---|
| `/v1/status` | computed | 200/23ms | 25ms | 91ms | 92ms | 93ms | 200:36 | 0 | 36/36 | **GREEN** |
| `/v1/metrics` | computed | 200/27ms | 24ms | 34ms | 36ms | 37ms | 200:36 | 0 | 36/36 | **GREEN** |
| `/v1/about` | computed | 404/24ms | - | - | - | - | 404:36 | 0 | n/a | **RED** |
| `/v1/guide` | computed | 404/23ms | - | - | - | - | 404:36 | 0 | n/a | **RED** |
| `/v1/quality` | bundled-file | 200/23ms | 26ms | 39ms | 117ms | 157ms | 200:36 | 0 | 36/36 | **GREEN** |
| `/v1/markets/search` | postgres | 200/386ms | 26ms | 40ms | 41ms | 41ms | 200:36 | 0 | 36/36 | **GREEN** |
| `/v1/markets/screen` | postgres | 200/578ms | 26ms | 37ms | 47ms | 52ms | 200:36 | 0 | 36/36 | **GREEN** |
| `/v1/markets/equivalents` | postgres | 200/24ms | 25ms | 37ms | 38ms | 38ms | 200:36 | 0 | 36/36 | **GREEN** |
| `/v1/markets/matched` | postgres | 200/114ms | 122ms | 432ms | 466ms | 475ms | 200:36 | 0 | 36/36 | **AMBER** |
| `/v1/markets/kalshi:KXATPGTOTAL-26JUN29COLFIL-41/equivalents` | postgres | 200/45ms | 41ms | 57ms | 65ms | 69ms | 200:36 | 0 | 36/36 | **GREEN** |
| `/v1/markets/kalshi:KXATPGTOTAL-26JUN29COLFIL-41/rules` | postgres | 200/37ms | 39ms | 48ms | 50ms | 51ms | 200:36 | 0 | 36/36 | **GREEN** |
| `/v1/markets/kalshi:KXATPGTOTAL-26JUN29COLFIL-41/core` | postgres | 200/29ms | 30ms | 46ms | 48ms | 48ms | 200:36 | 0 | 36/36 | **GREEN** |
| `/v1/markets/kalshi:KXATPGTOTAL-26JUN29COLFIL-41/related` | postgres | 200/35ms | 37ms | 53ms | 58ms | 61ms | 200:36 | 0 | 36/36 | **GREEN** |
| `/v1/markets/whale-trades` | live-venue-proxy | 200/64ms | 23ms | 38ms | 40ms | 40ms | 200:36 | 0 | 36/36 | **GREEN** |
| `/v1/markets/kalshi:KXATPGTOTAL-26JUN29COLFIL-41/book` | live-venue-proxy | 200/54ms | 24ms | 38ms | 39ms | 39ms | 200:36 | 0 | 36/36 | **GREEN** |
| `/v1/markets/polymarket:2703378/book` | live-venue-proxy | 200/134ms | 24ms | 124ms | 126ms | 127ms | 200:36 | 0 | 36/36 | **GREEN** |
| `/v1/markets/kalshi:KXATPGTOTAL-26JUN29COLFIL-41/trades` | live-venue-proxy | 200/62ms | 25ms | 68ms | 69ms | 70ms | 200:36 | 0 | 36/36 | **GREEN** |
| `/v1/markets/kalshi:KXATPGTOTAL-26JUN29COLFIL-41/oi` | live-venue-proxy | 200/54ms | 25ms | 36ms | 40ms | 41ms | 200:36 | 0 | 36/36 | **GREEN** |
| `/v1/markets/kalshi:KXATPGTOTAL-26JUN29COLFIL-41/ohlcv` | live-venue-proxy | 200/55ms | 44ms | 55ms | 225ms | 312ms | 200:36 | 0 | 36/36 | **GREEN** |
| `/v1/markets/polymarket:2703378/holders` | live-venue-proxy | 200/166ms | 23ms | 44ms | 219ms | 302ms | 200:36 | 0 | 36/36 | **GREEN** |
| `/v1/traders/leaderboard` | live-venue-proxy | 404/21ms | - | - | - | - | 404:36 | 0 | n/a | **RED** |
| `/v1/traders/0x2117ae94a97d69b78cbc81b6680a62deb1955c26` | live-venue-proxy | 404/24ms | - | - | - | - | 404:36 | 0 | n/a | **RED** |

## Notes

- `/v1/status` (computed): Exempt from the per-IP rate limiter (keyless allowlist).
- `/v1/metrics` (computed): Exempt from the per-IP rate limiter (keyless allowlist).
- `/v1/about` (computed): Exempt from the per-IP rate limiter (keyless allowlist).
- `/v1/guide` (computed): Exempt from the per-IP rate limiter (keyless allowlist).
- `/v1/markets/search` (postgres): q=election.
- `/v1/markets/polymarket:2703378/holders` (live-venue-proxy): Polymarket-only.
- `/v1/traders/leaderboard` (live-venue-proxy): Observed 404 on live prod during discovery probe despite being a registered route.
- `/v1/traders/0x2117ae94a97d69b78cbc81b6680a62deb1955c26` (live-venue-proxy): Observed 404 on live prod during discovery probe despite being a registered route.

## 5 slowest endpoints (by p95)

- `/v1/markets/matched` — p95 432ms (postgres)
- `/v1/markets/polymarket:2703378/book` — p95 124ms (live-venue-proxy)
- `/v1/status` — p95 91ms (computed)
- `/v1/markets/kalshi:KXATPGTOTAL-26JUN29COLFIL-41/trades` — p95 68ms (live-venue-proxy)
- `/v1/markets/kalshi:KXATPGTOTAL-26JUN29COLFIL-41/equivalents` — p95 57ms (postgres)

## RED verdicts

- `/v1/about` — zero 200s — all calls returned [404]
- `/v1/guide` — zero 200s — all calls returned [404]
- `/v1/traders/leaderboard` — zero 200s — all calls returned [404]
- `/v1/traders/0x2117ae94a97d69b78cbc81b6680a62deb1955c26` — zero 200s — all calls returned [404]
