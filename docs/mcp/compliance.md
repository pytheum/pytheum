# pytheum MCP — Compliance, Limits & Disclosures

Read this before relying on the server in production or listing it publicly.

## Read-only & safety posture

- **Every tool is read-only.** No tool places, modifies, or cancels orders;
  there are no trading credentials anywhere in the server. All 22 tools carry
  `readOnlyHint: true`, `destructiveHint: false`. None requires a confirmation
  prompt.
- **Annotations are hints, not guarantees.** Per the MCP specification, a client
  must not treat `readOnlyHint` as a security control — it is advisory metadata.
  Real isolation (network policy, sandboxing) remains the client's responsibility.
- **No internal-URL leakage.** Upstream errors are converted to structured
  `{error, hint}` objects; the internal REST origin is never echoed to the agent.
- **Prompt-injection caution.** Tool *responses* include third-party content
  (news/social snapshots, market titles, trader pseudonyms). Treat that text as
  untrusted data, not instructions — the standard cross-server MCP guidance.

## Rate limits (hosted connector)

The hosted endpoint `https://api.pytheum.com/mcp` is a free public connector with
a **per-IP token-bucket** limit:

- Sustained **60 requests/minute/IP**, burst bucket **60** (configurable via
  `PYTHEUM_MCP_RL_PER_MIN` / `PYTHEUM_MCP_RL_BURST` on a self-hosted deploy).
- Over-limit requests get **HTTP 429** with `{"error":"rate_limited",
  "retry_after_s":5}` and a `Retry-After: 5` header. Back off and retry.
- The limiter is per-process and the service runs single-process by design; do
  not assume higher effective limits.

The **local offline server** (`pytheum serve`) defaults to a per-IP **120
requests/minute** limit (burst 120) so a *public* self-host is throttled out of the
box too; access stays keyless (no API key required). For a single-user / offline
deploy set `PYTHEUM_RATE_LIMIT_PER_MIN=0` to disable it — it's yours.

## Data freshness & point-in-time disclosure

Every response discloses its source. Caching windows by tool family:

| Surface | Source field | Server-side cache |
|---|---|---|
| Orderbook | `live` | ~2 s |
| Recent trades | `live` | ~10 s |
| Open interest | `live` | ~30 s |
| Whale trades | `live` | ~30 s |
| Leaderboard | `live` | ~300 s |
| Trader profile / holders | `live` | ~60 s |
| OHLCV / history | `pit_archive` \| `venue_live` \| `mixed` | PIT capture (no lookahead) |
| Equivalence / matched / rules / related | bundled dataset (`dataset_version`) | dataset snapshot |

- **Live quotes are not a consolidated feed.** A quote may be a *parked wall* (a
  resting limit order behind a tight spread). Tools surface `is_parked_wall` and
  `last_move_age_s`; do not treat a frozen quote as a tradeable price or rank a
  cross-venue gap off it.
- **Cross-venue pairs are settlement-verified**, with the deciding `method` and a
  `confidence` disclosed per pair (deterministic structural methods carry `null`
  confidence by design — exact, not scored). `fungible_only: true` restricts to
  deterministic/structural/human-adjudicated pairs.
- **Dataset version** is reported by `t_status` and on every dataset-backed
  response; pin to it for reproducibility.

## Not financial advice — "substrate, not signal"

pytheum surfaces verified market structure (equivalence, rules, books, flow). It
is **substrate, not signal**: it does not recommend trades, predict outcomes, or
size positions. A reported `net_edge` is a fee-aware arithmetic relationship
between two live quotes at fetch time, not an executable guarantee — settlement
semantics, slippage, parked liquidity, venue time-skew, and lock-up horizon all
apply. Nothing here is financial advice. Verify resolution rules
(`t_market_rules`) and live executability before acting.

## Licensing

- **Code** (the `pytheum` library and MCP server): **MIT**.
- **Datasets** (settlement-verified cross-venue pairs in `datasets/`):
  **CC-BY-4.0** — attribution required. Attribute as: *"pytheum verified
  prediction-market graph (CC-BY-4.0)."* See `datasets/README.md` for the
  artifact schema and the version marker pinned at the data release.

## Registry-listing checklist (before going public)

To publish to the official MCP Registry, ship a `server.json` (template:
[`server.json`](server.json)) and verify:

- [ ] **`name`** in reverse-DNS form tied to a namespace you can prove
      (`io.github.pytheum/pytheum` via the GitHub account, or `com.pytheum/pytheum`
      via DNS/HTTP challenge on the domain).
- [ ] **`description`**, **`version`** (semver), **`$schema`** present.
- [ ] **`repository`** + **`websiteUrl`** populated.
- [ ] **`remotes[]`** entry: `type: "streamable-http"`, `url:
      "https://api.pytheum.com/mcp"`.
- [ ] **`packages[]`** entry for the local install: `registryType: "pypi"`,
      `identifier: "pytheum"`, stdio transport — once the PyPI package and a stdio
      entrypoint are published. (Today the package ships the streamable-HTTP MCP
      via `pytheum serve --mcp`; a `console_scripts` stdio entrypoint, e.g.
      `pytheum-mcp = pytheum.mcp.server:main`, should be added to `pyproject.toml`
      for a clean `pypi` package listing — `server.main()` already runs stdio.)
- [ ] Public accessibility: the remote endpoint is reachable and not
      network-restricted (registry policy excludes private servers).

> **Gap to close before listing:** add the stdio `console_scripts` entrypoint
> (`pytheum-mcp`) so the `pypi` package entry in `server.json` is launchable by a
> client out of the box. The remote (hosted) entry is listable today.
