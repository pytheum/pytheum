# pytheum MCP — Tool Reference

Complete reference for all 27 tools exposed by the pytheum MCP server. Each entry
gives the tool's purpose, parameters, return shape, MCP annotations, when to use
it, whether it works offline, and a real example call + response.

## Conventions

- **`market_ref`** is always **venue-prefixed**: `kalshi:<ticker>`,
  `polymarket:<id|slug>`, `manifold:<id>`, or a full market URL. A bare id
  (`KXNBA-26-NYK`, `558936`) is rejected with `{error, hint}` — discover ids with
  `t_screen` or `t_find_markets`. Whitespace and prefix case are normalized.
- **Errors are informative, never raw.** A bad ref / out-of-range param returns a
  structured `{error, hint, ...}` object (HTTP 200), never an exception or a
  leaked internal URL. A silently-empty result is avoided: typo'd enums, inverted
  windows, and unparseable dates all error rather than returning the unfiltered
  universe.
- **Annotations.** Every tool is **`readOnlyHint: true`**, **`destructiveHint:
  false`**, **`openWorldHint: true`** (tools that fetch live venue data reach
  external systems). `idempotentHint` is **true** for the deterministic
  dataset/live-snapshot reads and **false** for time-windowed tape reads (a later
  call returns newer trades). The per-tool **Annotations** line notes any
  exception. No tool is destructive; none requires confirmation.
- **Mode.** `Offline` = served locally by `pytheum serve` from dataset export
  files you provide via `PYTHEUM_EQUIVALENCE_PATH` / `PYTHEUM_RELATED_PATH` —
  the package ships no data; without those files the dataset tools return empty
  results with a `file_missing` flag (live price fields are `null` offline).
  `Local` = computed in-process, no data or network at all. `Hosted` = requires
  `https://api.pytheum.com` (venue fetch, embeddings, or PIT store).

> Note: these annotations are documented here and in `server.json`. They are
> *hints* — per the MCP spec, clients must not treat them as security guarantees.
> Their practical value: a client may skip the confirmation dialog for these
> read-only tools and retry idempotent ones safely.

---

## Meta & onboarding

### `t_guide`

Self-onboarding playbook for an agent landing on pytheum cold — **call this
first** if you're unsure where to start. Computed locally, no network.

**Use when:** first contact with the server; you want the operating rules, the
tool inventory grouped by job, and step recipes for common goals.

| Param | Type | Default | Required |
|---|---|---|---|
| _(none)_ | | | |

**Mode:** Local · **Annotations:** readOnly, idempotent

**Returns:** `summary` (what pytheum is), `principles` (operating rules — e.g.
`market_ref` must be venue-prefixed; equivalence is the core; confirm
settlement + staleness before trading a spread; this server is read-only),
`conventions` (the `market_ref` format + the `{ok, command, data, meta}`
envelope contract), `tool_groups` (the full tool inventory grouped by job:
health / discover / market_detail / cross_venue_equivalence / microstructure /
flow_and_traders / events_and_batch), `workflows` (ordered step recipes:
find + validate a cross-venue arb, check if a market exists on the other venue,
research one market, find today's movers).

### `t_about`

Who Pytheum is, what the data covers, why it exists, and who is building it.
Computed locally, no network.

**Use when:** an agent or user asks "what is this server / who runs it / what
does the dataset cover".

| Param | Type | Default | Required |
|---|---|---|---|
| _(none)_ | | | |

**Mode:** Local · **Annotations:** readOnly, idempotent

**Returns:** mission, data-coverage summary, team/contact pointers — the same
payload as `GET /v1/about`.

### `t_status`

Service health + dataset summary snapshot. Keyless.

**Use when:** first call of a session — confirm the service is up and the dataset
is fresh before issuing market queries.

| Param | Type | Default | Required |
|---|---|---|---|
| _(none)_ | | | |

**Mode:** Offline · **Annotations:** readOnly, idempotent

**Returns:** `platforms` (per-venue count + last_updated + `ok`/`stale`; omitted
when the server lacks DAO-backed venue stats), `equivalence` (pairs_loaded +
dataset_version), `related` (pairs_loaded), `service` (version + now).

```jsonc
// t_status()
{
  "equivalence": { "pairs_loaded": 142179, "dataset_version": "2026-06-28T21:40:00Z" },
  "related":     { "pairs_loaded": 1097 },
  "platforms": {
    "kalshi":     { "count": 45000, "last_updated": "2026-06-14T08:10:00Z", "freshness": "ok" },
    "polymarket": { "count": 90000, "last_updated": "2026-06-13T02:00:00Z", "freshness": "stale" }
  },
  "service": { "version": "0.0.1", "now": "2026-06-14T09:00:00Z" }
}
```

---

### `t_quality`

Dataset quality + integrity transparency — the "verify before you pay"
artifact. Keyless; every number is **derived from the loaded equivalence
dataset**, none asserted.

**Use when:** you (or a buyer/agent) want to know how much of the pair set is
structurally guaranteed vs LLM-judged before trusting a pair.

| Param | Type | Default | Required |
|---|---|---|---|
| _(none)_ | | | |

**Mode:** Offline · **Annotations:** readOnly, idempotent

**Returns:** `pairs_total` + `dataset_version`; `tiers` (`fungible` =
deterministic / structural / human-reviewed settlement-verified vs `judged` =
LLM-adjudicated, each with pairs + pct); `by_method` / `by_bet_type`
composition + `bet_types_total`; `integrity` (the build-time invariants
enforced before the dataset ships — 1:1, single-slice-per-id, line-invariant,
abbrev/name-alignment, same-city); `precision` (per-tier posture;
`audited_pct` is deliberately `null` — a labeled-sample precision % is
published separately, not asserted here).

---

## Cross-venue equivalence

### `t_equivalent_markets`

Find the SAME market on the other venue from the 136k-pair settlement-verified
equivalence dataset, with both venues' live prices and the cross-venue spread.

**Use when:** you have one market and want its exact counterpart on the other
venue (not a fuzzy title match).

| Param | Type | Default | Required |
|---|---|---|---|
| `market_ref` | string | — | yes |

**Mode:** Offline (live prices null offline) · **Annotations:** readOnly, idempotent

**Returns:** `market` (focal metadata), `equivalents[]` (counterparts with live
`implied_yes`/`book`/`volume` when in the store), `cross_venue`
(`kalshi_implied`, `pm_implied`, `spread` = kalshi − pm), `meta`
(`pairs_loaded`, `dataset_version`, `matched_via`). Missing file → empty
`equivalents` + `meta.degraded: true`.

```jsonc
// t_equivalent_markets("kalshi:KX-TEST-YES")
{
  "market": { "id": "kalshi:KX-TEST-YES", "venue": "kalshi", "question": "Will Test happen?" },
  "equivalents": [
    { "id": "polymarket:12345", "venue": "polymarket", "bet_type": "event",
      "confidence": 1.0, "implied_yes": 0.61, "book": { "bid": 0.60, "ask": 0.62 } }
  ],
  "cross_venue": { "kalshi_implied": 0.65, "pm_implied": 0.61, "spread": 0.04 },
  "meta": { "pairs_loaded": 142179, "dataset_version": "2026-06-28T21:40:00Z", "matched_via": "kalshi_ticker" }
}
```

---

### `t_matched_pairs`

Browse the verified cross-venue matched-pairs dataset (136k pairs) with both
venues' live prices and the cross-venue spread per pair.

**Use when:** you want to *survey* matched pairs by sport / bet type / text, or
sort by the biggest cross-venue disagreement (`sort_by: "spread"` = arbitrage
radar). For one known market use `t_equivalent_markets`.

| Param | Type | Default | Required |
|---|---|---|---|
| `bet_type` | string | — | no — `sports` group or specific (`moneyline`, `total`, `spread`, `tennis_ml`, `event`, …) |
| `query` | string | — | no — free-text over titles |
| `sort_by` | string | `volume` | no — `volume` \| `spread` \| `confidence` |
| `limit` | int | 25 | no — ≥1, capped 200 |
| `league` | string | — | no — e.g. `NBA`; rows without a league field are excluded |
| `date` | string | — | no — `YYYY-MM-DD`; rows without a game_date are excluded |
| `fungible_only` | bool | false | no — deterministic/structural pairs only |

**Mode:** Offline (live prices null offline) · **Annotations:** readOnly, idempotent

**Returns:** rows with both venues' prices + cross-venue spread; `meta`
(`leagues_available` ≤50 when present, `fungible_excluded` count).

```jsonc
// t_matched_pairs(bet_type="moneyline", league="NBA", sort_by="spread", limit=2)
{
  "rows": [
    { "bet_type": "moneyline", "league": "NBA", "game_date": "2026-06-14",
      "kalshi": { "id": "kalshi:KX-NBA-LAL-BOS", "implied_yes": 0.58 },
      "polymarket": { "id": "polymarket:10001", "implied_yes": 0.54 },
      "spread": 0.04, "method": "game_title_match", "confidence": null }
  ],
  "total": 1,
  "meta": { "leagues_available": ["MLB", "NBA", "NFL", "NHL"], "fungible_excluded": 0 }
}
```

---

### `t_market_rules`

Resolution rules text for a market AND its settlement-verified cross-venue
equivalent, side by side, with deadline comparison.

**Use when:** before treating two venues' prices as comparable — small wording
differences (strict-vs-inclusive thresholds, different settlement sources,
deadline gaps) make seemingly identical markets resolve differently.

| Param | Type | Default | Required |
|---|---|---|---|
| `market_ref` | string | — | yes |

**Mode:** Offline · **Annotations:** readOnly, idempotent

**Returns:** `market` (focal `resolution` text + `resolution_at` + `url`),
`equivalent` (same fields for the verified counterpart; `null` if no pair),
`comparison` (`deadlines.kalshi`/`.polymarket`, `same_deadline_day` bool-or-null,
`confidence`, `method`), `meta` (`pairs_loaded`, `dataset_version`,
`matched_via`).

```jsonc
// t_market_rules("kalshi:KX-TEST-YES")
{
  "market": {
    "id": "kalshi:KX-TEST-YES", "venue": "kalshi",
    "resolution": "This market resolves YES if Test happens before Dec 31 2026.",
    "resolution_at": "2026-12-31T00:00:00+00:00", "url": "https://kalshi.com/markets/kx-test-yes"
  },
  "equivalent": {
    "id": "polymarket:12345", "venue": "polymarket",
    "resolution": "This market will resolve to YES if Test happens by December 31, 2026.",
    "resolution_at": "2026-12-31T12:00:00+00:00", "url": "https://polymarket.com/event/will-test-happen"
  },
  "comparison": {
    "deadlines": { "kalshi": "2026-12-31T00:00:00+00:00", "polymarket": "2026-12-31T12:00:00+00:00" },
    "same_deadline_day": true, "confidence": 1.0, "method": "blocked_deterministic"
  },
  "meta": { "pairs_loaded": 142179, "dataset_version": "2026-06-28T21:40:00Z", "matched_via": "kalshi_ticker" }
}
```

---

### `t_related_markets`

Correlated cross-venue markets that are NOT settlement-equivalent (different
bands / sources / deadlines) — hedge discovery, not arbitrage.

**Use when:** you want a correlated position to contextualize or hedge a market
but no exact same-question pair exists. For true same-market pairs use
`t_equivalent_markets`.

| Param | Type | Default | Required |
|---|---|---|---|
| `market_ref` | string | — | yes |
| `include_hyperliquid` | boolean | `false` | no |

**Mode:** Offline · **Annotations:** readOnly, idempotent

**Returns:** a list of related markets, each with the relation type, both venues'
bands, and a `basis` note spelling out exactly how settlement differs.

With `include_hyperliquid: true` (opt-in — the default response is unchanged),
the payload additionally carries:

- `hyperliquid_related` — rows verbatim from the Hyperliquid related tier
  (loaded from `PYTHEUM_HL_RELATED_PATH`), looked up by this market's
  identifiers. Each row is venue-explicit: a 2-element `legs` list (exactly one
  `hyperliquid` leg plus one `kalshi`-or-`polymarket` leg; every leg carries
  `venue`/`ref`/`native_id`/`title`, Polymarket legs add `gamma_id` + `slug`,
  the HL leg adds `implied_yes` (0–1) + `as_of` (ISO)), flattened
  `<venue>_ref`/`<venue>_native_id`/`<venue>_title` fields, plus
  `tier`/`relation`/`settlement`/`basis_note` metadata.
- `hyperliquid_note` — a standing caveat: HL leg prices (`implied_yes`/`as_of`)
  are a **mint-time daily snapshot, not live quotes** — treat cross-venue
  spreads involving the HL leg as indicative, not executable.
- When the HL dataset file is missing the tool degrades (never errors):
  `hyperliquid_related: []` + `hyperliquid_file_missing: true`.

```jsonc
// t_related_markets("kalshi:KXBTC-25DEC-100K")
{
  "related": [
    { "venue": "polymarket", "id": "polymarket:99001",
      "relation": "same_underlying_different_band",
      "bands": { "kalshi": ">= $100,000 by Dec 31", "polymarket": ">= $90,000 by Dec 31" },
      "basis": "Different strike; correlated but not fungible — a hedge, not a lock." }
  ],
  "count": 1
}
```

```jsonc
// t_related_markets("kalshi:KXBTC-25DEC-100K", include_hyperliquid=true) — extra fields
{
  // ...the normal payload above, plus:
  "hyperliquid_related": [
    { "tier": "related", "relation": "crypto_threshold_in_band_divergent",
      "legs": [
        { "venue": "kalshi", "ref": "kalshi:KXBTC-25DEC-100K",
          "native_id": "KXBTC-25DEC-100K", "title": "Bitcoin above $100k on Dec 31?" },
        { "venue": "hyperliquid", "ref": "hyperliquid:BTC-100K-DEC",
          "native_id": "BTC-100K-DEC", "title": "BTC >= $100k Dec 31",
          "implied_yes": 0.61, "as_of": "2026-07-01T04:00:00Z" }
      ],
      "asset": "BTC", "basis_note": "Same threshold; HL settles on a different index print." }
  ],
  "hyperliquid_note": "Hyperliquid leg prices (implied_yes/as_of) are a mint-time daily snapshot, not live quotes — treat cross-venue spreads involving the HL leg as indicative, not executable."
}
```

---

### `t_find_divergences`

Cross-venue divergence scanner: verified same-question pairs joined to live books,
sorted **clean-first** (any row with `warnings` sorts after every clean row), then
by **annualized** net-of-fees locked edge (capital efficiency — a small near-term
lock outranks a bigger one tied up for years).

**Use when:** you want a ranked, fee-aware view of where the two venues disagree
on the same question, with settlement rules inlined.

| Param | Type | Default | Required |
|---|---|---|---|
| `min_net_edge` | float | 0.0 | no — filter on raw net edge (e.g. 0.03) |
| `limit` | int | 10 | no — ≥1, max pairs returned |
| `include_rules` | bool | true | no — bundle each pair's settlement text (400-char truncated) |
| `fungible_only` | bool | false | no — deterministic/structural pairs only |
| `include_warned` | bool | true | no — keep warned rows (demoted + labeled); `false` filters them out |
| `include_depth` | bool | true | no — page-local live-depth overlay (sizes `max_lockable_notional`); `false` skips all book fetches |

**Mode:** Hosted (needs live book join; offline returns pairs with no edge) ·
**Annotations:** readOnly, **not idempotent** (live books move between calls)

**Returns:** `divergences[]` — each with `net_edge`, `annualized_net_edge`,
`lock_days`, `matched_by`, `match_confidence`, `bet_type`, `title_similarity`,
`either_leg_parked` (true = a frozen parked-wall quote → the edge is a ghost),
`warnings[]` (first-class honesty labels: `resolution_mismatch` — the legs'
resolution dates disagree >1d; `either_leg_parked`; `stale_quote` — a leg's price
frozen >3600s; `near_resolution` — a leg <1 day from resolution;
`depth_unverified` — fillable size unknown), `max_lockable_notional` (depth-capped
fillable USD at the quoted top-of-book: min over legs of executable-side size ×
price on the edge's direction; **null** when either leg's book carries no size —
unverified, never guessed) + `notional_basis` (documents the computation), both
legs (`a`=Kalshi, `b`=Polymarket) with `is_parked_wall`/`last_move_age_s`, and
`resolution.{kalshi,polymarket}` when `include_rules`. Plus `pairs_scanned`,
`orientation_excluded`, `parked_excluded`, `suspect_excluded`, `extreme_excluded`,
`warned_filtered` (rows dropped when `include_warned=false`), `depth_overlaid`
(rows sized by the live-depth overlay), `ranked_by`, `note`.

**Ordering semantics:** rows with ANY warning sort AFTER all clean rows; within
each group the annualized-edge (fallback raw-edge) order is kept. **A large edge
with warnings is usually quote noise** — a stale, parked, or hours-from-resolution
leg, not free money (a live probe saw a flagged #1 "edge" collapse 23.1c → −4c on
requote). See `warnings` before acting.

**Live-depth overlay (`include_depth`, default true) — page-local:** the
equivalents-route books carry bid/ask but no sizes, so without the overlay every
row ships `max_lockable_notional: null` + `depth_unverified`. After the final
sort + `limit` slice, both legs of each returned row get a live top-of-book fetch
(`/v1/markets/{ref}/book?depth=1`, coalesced ~2s server-side) — bounded cost
≤ 2×`limit` GETs under one ~4s overall deadline; **rows beyond `limit` are never
fetched**. Where both legs' sizes parse, the sizes are overlaid onto the row's
quoted books (prices unchanged), `max_lockable_notional` is recomputed,
`notional_basis` notes the sizes came from a live scan-time fetch, the
`depth_unverified` warning is removed, and the page is re-sorted — a row that
became clean floats above warned rows *within the page*. A failed/timed-out leg
leaves its row's honest null untouched. `depth_overlaid` counts updated rows.

```jsonc
// t_find_divergences(min_net_edge=0.02, limit=1)
{
  "divergences": [
    { "net_edge": 0.031, "annualized_net_edge": 0.42, "lock_days": 28,
      "matched_by": "human_adjudicated", "match_confidence": 1.0, "bet_type": "event",
      "title_similarity": 0.91, "either_leg_parked": false,
      "warnings": [],
      "max_lockable_notional": 812.5,
      "notional_basis": "min over legs of top-of-book size x executable price at the quoted books (buy-YES leg: ask_size x ask; buy-NO leg: bid_size x (1-bid)), taken on the cheaper (edge) direction; top-of-book level only (no deeper levels fetched); fees not netted",
      "a": { "market_id": "kalshi:KXFED-25-JUL", "venue": "kalshi", "implied_yes": 0.66,
             "book": { "bid": 0.65, "ask": 0.67, "bid_size": 1500, "ask_size": 1250 },
             "is_parked_wall": false, "last_move_age_s": 120 },
      "b": { "market_id": "polymarket:558936", "venue": "polymarket", "implied_yes": 0.62,
             "book": { "bid": 0.61, "ask": 0.63, "bid_size": 2083, "ask_size": 900 },
             "is_parked_wall": false, "last_move_age_s": 45 },
      "resolution": { "kalshi": "Resolves YES if the FOMC…", "polymarket": "Resolves Yes if the Fed…" } }
  ],
  "pairs_scanned": 150, "orientation_excluded": 12, "parked_excluded": 3, "suspect_excluded": 1,
  "warned_filtered": 0, "depth_overlaid": 1,
  "ranked_by": "clean-first (rows with any `warnings` sort after clean rows), then annualized_net_edge (capital-efficiency; falls back to net_edge when horizon unknown)"
}
```

---

## Discovery & context

### `t_find_markets`

Semantic search: find prediction markets matching a free-form text query (article
body / headline / question).

**Use when:** you have unstructured text (a news story, a thesis) and want the
markets it maps to. For structured filtering use `t_screen`.

| Param | Type | Default | Required |
|---|---|---|---|
| `query` | string | — | yes — free text |
| `limit` | int | 50 | no |
| `group_by` | string | `bundle` | no — `bundle` (one row/event) \| `none` |
| `venue` | string\|list | — | no — `kalshi`\|`polymarket`\|`manifold` (aliases `poly`, `all`/`both`); unknown → error |
| `min_similarity` | float | — | no — 0.0–1.0 cosine threshold |
| `exclude_stale` | bool | false | no — drop resolved/expired |

**Mode:** Hosted · **Annotations:** readOnly, idempotent

**Returns:** ranked `markets[]` with `implied_yes`/`book`/`liquidity`/
`resolution`/`resolution_status`/`condition_id`/`event_key`/`is_play_money`;
crypto rows also carry `spot_ref` (live underlying USD spot).

```jsonc
// t_find_markets("Fed cuts rates in July", venue="kalshi", limit=1)
{
  "markets": [
    { "id": "kalshi:KXFED-25-JUL", "venue": "kalshi", "question": "Fed July rate cut?",
      "implied_yes": 0.66, "book": { "bid": 0.65, "ask": 0.67 }, "liquidity_usd": 48000,
      "resolution_status": "active", "is_play_money": false, "similarity": 0.78,
      "taker_fee_bps": 157.1, "volume_usd_norm": 31200.0 }
  ],
  "count": 1
}
```

---

### `t_screen`

Structured (non-semantic) market screen — filter by venue/status/volume/
liquidity/resolution window; sort by volume \| liquidity \| resolution \| move.

**Use when:** you want "top movers today" (`sort_by: "move"`), the most liquid
markets, or anything resolving in a window — one call replaces N semantic
searches.

| Param | Type | Default | Required |
|---|---|---|---|
| `venues` | string\|list | — | no — as `t_find_markets`; unknown → error |
| `status` | string | `active` | no — `active`\|`resolved`\|`closed`; `all`/`any` → every status |
| `min_volume` / `max_volume` | float | — | no — inverted window → error |
| `min_liquidity` | float | — | no |
| `resolves_before` / `resolves_after` | string | — | no — ISO-8601; bad date errors; after>before → error |
| `sort_by` | string | `volume` | no — `volume`\|`liquidity`\|`resolution`\|`move` |
| `limit` | int | 50 | no |
| `exclude_stale` | bool | false | no |

**Mode:** Hosted (degraded offline) · **Annotations:** readOnly, idempotent

**Returns:** rows with `implied_yes`/`book`/`resolution_status`/`condition_id`/
`bundle_outcomes`, plus quote-staleness inline (`last_move_age_s`,
`is_parked_wall`), `move_24h`/`move_7d`; crypto rows carry `spot_ref`. Volume sort
re-orders onto one cross-venue axis (`volume_usd_norm`).

```jsonc
// t_screen(venues="polymarket", sort_by="move", limit=1)
{
  "markets": [
    { "id": "polymarket:771203", "venue": "polymarket", "question": "…",
      "implied_yes": 0.41, "move_24h": 0.18, "book": { "bid": 0.40, "ask": 0.42 },
      "last_move_age_s": 90, "is_parked_wall": false, "resolution_status": "active" }
  ],
  "count": 1,
  "meta": { "sorted_by": "volume_usd_norm" }
}
```

---

### `t_search_markets`

Text search over market **titles** across venues — the cheap, exact complement
to `t_find_markets`' semantic search. AND-matches the query's title tokens
(`super bowl winner` must contain all of super/bowl/winner) and ranks by
volume. Non-semantic: it nails exact terms a paraphrase-based kNN can miss (a
ticker like `KXBTC`, a player name, `H5N1`) but will **not** find conceptual
paraphrases — for "markets like this article/headline" use `t_find_markets`.
Keyless.

**Use when:** you know words that literally appear in the market's title.

| Param | Type | Default | Required |
|---|---|---|---|
| `q` | string | — | yes (non-empty) |
| `venue` | string \| list | all venues | no (`kalshi` \| `polymarket` \| `manifold`; aliases like `poly`, `all`/`both`; unknown venue errors) |
| `status` | string | `active` | no (`any`/`all` → every status) |
| `limit` | int 1–200 | 50 | no |

**Mode:** Hosted · **Annotations:** readOnly, idempotent

**Returns:** rows in the same triage shape as `t_screen`
(implied_yes / book / resolution / resolution_status / condition_id + the
verified `cross_venue` twin + quote-staleness flags) so you can size an edge
without a `t_get_market` round-trip. An empty result carries a `meta.hint`
distinguishing "no such title" from "you wanted a semantic match".

### `t_get_market`

Lean fetch of **one** market's core by ref — the fast "get this market" call
when an agent lands with a venue id or market URL and doesn't need the full
`t_market_context` payload (ladder + siblings + news).

**Use when:** resolving a known ref to price/book/status; drill into
`t_equivalent_markets` when `meta.has_equivalent` is true. Use
`t_market_context` for rules/ladder/siblings/news; `t_find_markets`/`t_screen`
to discover by query/filter.

| Param | Type | Default | Required |
|---|---|---|---|
| `market_ref` | string | — | yes (venue-prefixed ref, slug, market URL; a raw Kalshi ticker also resolves) |

**Mode:** Hosted · **Annotations:** readOnly, idempotent

**Returns:** `market` {id, venue, question, status, implied_yes, book
(bid/ask/spread/sizes), volume_usd, condition_id, resolution_status,
resolution_at, url, found} and `meta` {has_equivalent, matched_via,
pairs_loaded}. A market not in the store returns `market.found = false` +
`meta.degraded` rather than an error.

### `t_market_context`

News / social / macro events paired with a specific market, plus correlated
`sibling_markets` from the same event graph.

**Use when:** you want the prediction-market-native context behind one market —
what's driving it and which sibling legs co-move.

| Param | Type | Default | Required |
|---|---|---|---|
| `market_ref` | string | — | yes — outcome leg (best) or bundle/event parent |
| `limit` | int | 25 | no |

**Mode:** Hosted · **Annotations:** readOnly, idempotent

**Returns:** `market` metadata, ranked `context[]` (frozen snapshots: title, body,
url, published_at), `sibling_markets[]` with volume + implied odds. Each leg's
`flow_flag` is a **precomputed** positioning breadcrumb that can lag live flow —
confirm direction with `t_market_flow`. Bad ref → `{error, hint}`.

```jsonc
// t_market_context("polymarket:558936", limit=2)
{
  "market": { "id": "polymarket:558936", "venue": "polymarket", "question": "…",
              "implied_yes": 0.62, "flow_flag": "accumulating" },
  "context": [
    { "kind": "news", "title": "…", "url": "https://…", "published_at": "2026-06-14T07:00:00Z", "similarity": 0.81 }
  ],
  "sibling_markets": [
    { "id": "kalshi:KXFED-25-JUL", "venue": "kalshi", "volume_usd": 120000, "implied_yes": 0.66 }
  ]
}
```

---

### `t_bundle_context`

Events paired with any market inside a bundle (an event/group), deduplicated by
event.

**Use when:** you want event-level context across a whole ladder (e.g. a
presidential election, an NBA series) rather than one leg. For one market use
`t_market_context`.

| Param | Type | Default | Required |
|---|---|---|---|
| `bundle_ref` | string | — | yes — a group/event id (`polymarket:soccer`, `kalshi:KXNBA-26`) |
| `limit` | int | 50 | no |

**Mode:** Hosted · **Annotations:** readOnly, idempotent

**Returns:** deduplicated `context[]` (highest-similarity hit wins;
`matched_market_id` names the winning child) + the outcome ladder. Find bundle
ids via the `bundle_id` field on `t_screen`/`t_find_markets` rows. Bad ref →
`{error, hint}`.

---

### `t_context_batch`

Batch DIGEST of `t_market_context` for up to 25 markets in ONE call (avoids N
round trips).

**Use when:** you have a screen page of refs and want a lean context digest for
all of them at once. Drill into a single ref with `t_market_context` for the full
object.

| Param | Type | Default | Required |
|---|---|---|---|
| `market_refs` | list[string] | — | yes — non-empty list of venue-prefixed ids (deduped, capped 25) |
| `limit` | int | 8 | no — context items per ref |

**Mode:** Hosted · **Annotations:** readOnly, idempotent

**Returns:** `{results: {ref: digest}, count, ok_count, error_count}`. Each digest:
a market CORE (id/question/venue/implied_yes/book/volume_usd_norm/taker_fee_bps/
flow_flag/days_to_resolution/is_stale/resolution_status/market_archetype) + up to
3 context headlines + `sibling_markets_count`/`bundle_children_count`. Partial
failures don't sink the batch; a bad ref's entry is `{error, hint}`. Over-cap /
duplicate refs disclosed in a top-level `note`.

```jsonc
// t_context_batch(["kalshi:KXFED-25-JUL", "polymarket:558936"])
{
  "results": {
    "kalshi:KXFED-25-JUL": {
      "market": { "id": "kalshi:KXFED-25-JUL", "venue": "kalshi", "implied_yes": 0.66,
                  "market_archetype": "macro", "days_to_resolution": 28 },
      "context": [ { "kind": "news", "title": "…", "url": "https://…" } ],
      "sibling_markets_count": 4
    },
    "polymarket:558936": { "market": { "id": "polymarket:558936", "venue": "polymarket", "implied_yes": 0.62 } }
  },
  "count": 2, "ok_count": 2, "error_count": 0
}
```

---

### `t_event_related_markets`

Given a firehose `event_id` (looks like `evt_news_headline_…`), find the markets
it relates to.

**Use when:** you have a live-stream event id (from a `t_market_context` paired
event or the firehose) and want its markets. NOT a `market_ref` — passing one
returns a redirect hint to `t_market_context`.

| Param | Type | Default | Required |
|---|---|---|---|
| `event_id` | string | — | yes — `evt_…` (only the 24h rolling window resolves) |
| `limit` | int | 25 | no |

**Mode:** Hosted · **Annotations:** readOnly, idempotent

**Returns:** ranked related markets, or `{error, hint}` for an expired/wrong-type
id.

---

## Live market data

### `t_orderbook`

Live orderbook snapshot for a market.

**Use when:** you need real executable depth/top-of-book, not just an implied
price.

| Param | Type | Default | Required |
|---|---|---|---|
| `market_ref` | string | — | yes |
| `depth` | int | 20 | no — 1–200 price levels |

**Mode:** Hosted · **Annotations:** readOnly, **not idempotent** (live snapshot) ·
coalesced + cached ~2 s

**Returns:** `bids`/`asks` as `[[price, size], …]` in probability units [0,1] + a
top-of-book summary (bid, ask, spread, mid, sizes) + `source: "live"`. On venue
error → `source: "unavailable"`.

```jsonc
// t_orderbook("polymarket:558936", depth=2)
{
  "bids": [[0.61, 1200], [0.60, 3400]], "asks": [[0.63, 900], [0.64, 2100]],
  "summary": { "bid": 0.61, "ask": 0.63, "spread": 0.02, "mid": 0.62 },
  "venue": "polymarket", "source": "live"
}
```

---

### `t_recent_trades`

Recent trade tape for a market.

**Use when:** you want the actual prints (who traded at what), e.g. to confirm a
quote is live, not parked.

| Param | Type | Default | Required |
|---|---|---|---|
| `market_ref` | string | — | yes |
| `limit` | int | 50 | no — 1–1000 |

**Mode:** Hosted · **Annotations:** readOnly, **not idempotent** · coalesced +
cached ~10 s

**Returns:** `{trades: [{ts, price, size, side}, …], count, venue, source:"live"}`.
On venue error → `source: "unavailable"`.

---

### `t_open_interest`

Current open interest (total contracts/shares outstanding) for a market.

**Use when:** you want to gauge how much capital is committed and whether real
depth backs a quote.

| Param | Type | Default | Required |
|---|---|---|---|
| `market_ref` | string | — | yes |

**Mode:** Hosted · **Annotations:** readOnly, **not idempotent** · coalesced +
cached ~30 s

**Returns:** `{open_interest: float|null, venue, ref, source:"live"}`. On venue
error → `source: "unavailable"`.

---

### `t_ohlcv`

OHLCV candles for any Kalshi/Polymarket market — pytheum's own point-in-time
capture first (no lookahead, backtest-grade), venue candles as a disclosed
fallback.

**Use when:** you want a price series for charting or backtesting with a known
provenance.

| Param | Type | Default | Required |
|---|---|---|---|
| `market_ref` | string | — | yes |
| `interval` | string | `1h` | no — `1m`\|`5m`\|`15m`\|`1h`\|`1d` |
| `since` / `until` | string | last 7 days | no — ISO-8601 or Unix seconds |
| `limit` | int | 200 | no — 1–1000 |

**Mode:** Hosted · **Annotations:** readOnly, idempotent (fixed window)

**Returns:** `{market, interval, candles: [{t,o,h,l,c,v}], meta: {source:
pit_archive|venue_live|mixed, count, partial_last_bucket}}`. `v` is `null` when no
trade-count data. Invalid interval/range → `{error, hint}`.

```jsonc
// t_ohlcv("kalshi:KXFED-25-JUL", interval="1d", limit=2)
{
  "market": { "id": "kalshi:KXFED-25-JUL", "question": "…", "venue": "kalshi" },
  "interval": "1d",
  "candles": [
    { "t": "2026-06-28T00:00:00Z", "o": 0.62, "h": 0.67, "l": 0.61, "c": 0.66, "v": 18400 },
    { "t": "2026-06-13T00:00:00Z", "o": 0.66, "h": 0.68, "l": 0.64, "c": 0.65, "v": null }
  ],
  "meta": { "source": "pit_archive", "count": 2, "partial_last_bucket": true }
}
```

---

### `t_market_history`

PIT price + book history + derived moves (`move_1h`/`24h`/`7d`) — pytheum's own
point-in-time capture; tells you if a price is stale.

**Use when:** you want to know "is this price stale / how did it move." The
`staleness` + `moves` block answers that without the full tape.

| Param | Type | Default | Required |
|---|---|---|---|
| `market_ref` | string | — | yes — outcome leg (a bundle parent has no own series) |
| `limit` | int | 500 | no — ≥1, capped 2000 |
| `full` | bool | false | no — return the complete tape (500+ points); default downsamples to ~40 |

**Mode:** Hosted · **Annotations:** readOnly, **not idempotent** (new points
accrue)

**Returns:** `staleness` (`last_observed_age_s`, `last_move_age_s`,
`is_live_event`), the moves block, and a `points[]` array (downsampled by
default; `points_total` + `downsampled` disclose the thinning).

```jsonc
// t_market_history("polymarket:558936")
{
  "staleness": { "last_observed_age_s": 42, "last_move_age_s": 1180, "is_live_event": false },
  "moves": { "move_1h": -0.01, "move_24h": 0.05 },
  "points": [ { "t": "2026-06-14T08:00:00Z", "implied_yes": 0.61, "bid": 0.60, "ask": 0.62 } ],
  "points_total": 512, "downsampled": true
}
```

---

### `t_market_flow`

Wallet-level trade flow for a **Polymarket** market: net directional pressure,
whale concentration, largest recent positions.

**Use when:** you want to confirm *current* positioning direction (more
authoritative than the precomputed `flow_flag`).

| Param | Type | Default | Required |
|---|---|---|---|
| `market_ref` | string | — | yes — Polymarket outcome leg with a conditionId |
| `window_hours` | int | 24 | no — clamped 1–168 |

**Mode:** Hosted · **Annotations:** readOnly, **not idempotent** ·
Polymarket-only (a Kalshi ref → `coverage: "unavailable"`)

**Returns:** net flow, whale concentration, largest positions, and `coverage`
(`tracked` stored-aggregate \| `on_demand` live snapshot \| `unavailable`).

---

## Trader analytics — Polymarket-only

> Kalshi trades are anonymized, so no holder / leaderboard / wallet-flow
> equivalent exists on that venue. These four tools say so explicitly (they do not
> return empty or guess).

### `t_leaderboard`

Polymarket trader leaderboard.

**Use when:** you want the top traders by profit/volume for a period.

| Param | Type | Default | Required |
|---|---|---|---|
| `period` | string | `weekly` | no — `weekly` \| `monthly` |

**Mode:** Hosted · **Annotations:** readOnly, **not idempotent** · cached 300 s

**Returns:** `{period, traders: [{name, address, profit, volume, rank}], count,
source, venue}`. On venue error → `source: "unavailable"`.

---

### `t_trader_profile`

Polymarket trader profile — positions, recent activity, portfolio value in one
call.

**Use when:** you want to inspect a specific wallet/username's book.

| Param | Type | Default | Required |
|---|---|---|---|
| `wallet` | string | — | yes — 0x-hex address or Polymarket username |

**Mode:** Hosted · **Annotations:** readOnly, **not idempotent** · cached 60 s

**Returns:** `{wallet, positions[], activity[], value, meta}`. On venue error →
`source: "unavailable"`.

---

### `t_market_holders`

Holder breakdown for a Polymarket market — who holds YES/NO tokens and how much.

**Use when:** you want concentration/ownership of a specific market.

| Param | Type | Default | Required |
|---|---|---|---|
| `market_ref` | string | — | yes — `polymarket:…` |

**Mode:** Hosted · **Annotations:** readOnly, **not idempotent** · cached 60 s

**Returns:** `{holders: [{address, amount, outcome}], count, ref, source, venue}`.
On venue error → `source: "unavailable"`.

---

### `t_whale_trades`

Large-notional Polymarket trades where `notional_usd` (size × price) ≥ `min_usd`.

**Use when:** you want a feed of big prints across Polymarket (or filtered to one
market).

| Param | Type | Default | Required |
|---|---|---|---|
| `min_usd` | float | 500 | no — minimum notional USD |
| `limit` | int | 50 | no — 1–500 |
| `market_ref` | string | — | no — `polymarket:…` to filter to one market |

**Mode:** Hosted · **Annotations:** readOnly, **not idempotent** · cached 30 s

**Returns:** `{trades: [{ts, market, price, size, notional_usd, side, wallet,
pseudonym?}], count, min_usd, venue, source}`. On venue error → `source:
"unavailable"`.

```jsonc
// t_whale_trades(min_usd=5000, limit=1)
{
  "trades": [
    { "ts": "2026-06-14T08:55:00Z", "market": "polymarket:558936", "price": 0.62,
      "size": 12000, "notional_usd": 7440.0, "side": "buy", "wallet": "0xab…cd", "pseudonym": "WhaleHunter" }
  ],
  "count": 1, "min_usd": 5000, "venue": "polymarket", "source": "live"
}
```
