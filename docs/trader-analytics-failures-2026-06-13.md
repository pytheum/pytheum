# Trader-analytics endpoint failures — 2026-06-13

Four trader-analytics endpoints return empty/error output. Found during the
pre-cutover review of the refactor (validated locally against live
Supabase/Pinecone + live venue APIs). **None are deployed to prod yet** — these
are new endpoints from the trader-analytics commit (`d303b6b`), so there is no
production impact; this is a pre-launch fix list.

Repro environment: `pytheum-pit` server on `:8455` (serves the `pytheum`
handlers), pointed at live data; identical behavior via the MCP tools layer
(`pytheum.mcp.tools`) since the tools wrap these HTTP routes. The other ~18
serve-tier tools are at parity-or-better vs prod; these 4 are the outliers.

Each handler's own docstring states the INTENDED data path — the failures are
all "intended path not wired / field-mapping mismatch," not design gaps. Root
causes are in the `pytheum-core` data layer (venue clients / normalizers), not
the serve handlers.

---

## 1. Polymarket `/book` → `venue_unavailable`

**Symptom:** every Polymarket orderbook request errors; Kalshi works.

```
$ curl http://127.0.0.1:8455/v1/markets/polymarket:558940/book
{"error": "venue_unavailable", "detail": "no results for '/book' in polymarket-rest",
 "source": "unavailable", "venue": "polymarket", "ref": "polymarket:558940"}

# contrast — Kalshi book works (full depth ladder):
$ curl http://127.0.0.1:8455/v1/markets/kalshi:KXNBA-26-NYK/book
{"bids": [[0.8, 2939.68], [0.79, 409855.04], [0.78, 214269.33], ...], ...}
```
Reproducible on every Polymarket ref tried (558940 live, 30615). 100%, not transient.

**Intended path** (`src/pytheum/api/markets_book.py` docstring): "Polymarket:
resolve ref → CLOB token_id via Gamma, then `clob.get_book(token_id)`."

**Root cause (hypothesis):** the book query is being routed to the
`polymarket-rest` (Gamma) scope, which has no `/book` resource → `NoResults`
(`pytheum-core/src/pytheum_core/data/errors.py:101`). Polymarket order books
live on the **CLOB** (`clob.polymarket.com/book?token_id=…`), not Gamma. Either
(a) the Gamma→token_id resolution isn't returning a token_id so it falls through
to a rest query, or (b) `clob.get_book` isn't wired into the polymarket data
source. **Look in:** `pytheum-core` polymarket data source / CLOB client +
`markets_book.py:55` (`handle_market_book`, the polymarket branch).

---

## 2. `/oi` (open interest) → null on both venues

**Symptom:** `open_interest` is always null, Kalshi and Polymarket.

```
$ curl http://127.0.0.1:8455/v1/markets/kalshi:KXNBA-26-NYK/oi
{"open_interest": null, "venue": "kalshi", "ref": "kalshi:KXNBA-26-NYK", "source": "live"}

$ curl http://127.0.0.1:8455/v1/markets/polymarket:558940/oi
{"open_interest": null, "venue": "polymarket", "ref": "polymarket:558940", "source": "live"}
```
Reproducible on every market (incl. World Cup, NBA). `source:"live"` so the call
ran — the value just isn't populated.

**Intended path** (`markets_oi.py` docstring): "Kalshi: `KalshiRest.get_market(ticker)`
— extract `open_interest` field; Polymarket: resolve ref → condition_id, then
`data.get_open_interest([condition_id])`."

**Root cause (hypothesis):** Kalshi's `get_market` response carries open interest
(Kalshi exposes `open_interest`), and the SDK model HAS the field
(`pytheum-core/.../data/models.py:131 open_interest`, schema `003_markets_outcomes.sql:11`)
— so the field exists but the **extraction/mapping from the venue response isn't
populating it** (likely a key-name mismatch, e.g. `open_interest` vs a `*_fp`/nested
field on the new Kalshi schema). Polymarket's `data.get_open_interest` returns
null too. **Look in:** `pytheum-core` Kalshi `get_market` field mapping + the
polymarket `get_open_interest` query.

---

## 3. `/holders` → rows present but all fields null

**Symptom:** returns the right row count but every field is null.

```
$ curl http://127.0.0.1:8455/v1/markets/polymarket:558940/holders
{"holders": [{"address": null, "amount": null, "outcome": null},
             {"address": null, "amount": null, "outcome": null}],
 "count": 2, "ref": "polymarket:558940", "source": "live", "venue": "polymarket",
 "note": "Polymarket-only. Kalshi trades are anonymized."}
```
`count: 2` (so it's iterating the upstream response) but address/amount/outcome
all null → **the normalizer is reading the wrong keys.**

**Root cause (hypothesis):** `normalize_pm_holders`
(`src/pytheum/api/markets_holders.py:16` → `pytheum.trader.normalizers`) maps
fields that don't match the current Polymarket data-api holders response shape
(upstream key names changed, or nested under a different path). **Look in:**
`pytheum.trader.normalizers.normalize_pm_holders` vs a raw sample of the
Polymarket holders endpoint.

---

## 4. `whale-trades` → `market` and `wallet` null on every trade

**Symptom:** live trades return, but the two fields a trader most needs
(`market`, `wallet`) are null; only `pseudonym` is populated.

```
$ curl 'http://127.0.0.1:8455/v1/markets/whale-trades?min_usd=50&limit=3'
{"trades": [{"ts": "2026-06-13T18:13:55+00:00", "market": null, "price": 0.99,
  "size": 83.54, "notional_usd": 82.70, "side": "BUY", "wallet": null,
  "pseudonym": "Gargantuan-Obligation-Dancing"}],
 "count": 1, "min_usd": 50.0, "venue": "polymarket", "source": "live",
 "note": "Polymarket-only. Kalshi trades are anonymized."}
```
(At default `min_usd=500` the live window was empty — use a lower `min_usd` to
reproduce.) ts/price/size/notional/side/pseudonym populate; `market` + `wallet`
do not.

**Root cause (hypothesis):** the trade normalizer populates `pseudonym` but not
the `market` (conditionId → our market ref) or `wallet` (proxy/maker address)
joins — either the upstream keys aren't read or the conditionId→market
hydration isn't wired. `market: null` makes the feed un-attributable; `wallet:
null` removes smart-money tracking. **Look in:** `markets_whale_trades.py:61`
(`handle_market_whale_trades`) + the polymarket trades normalizer in
`pytheum.trader`.

---

## Summary

| # | Endpoint | Symptom | Likely fix location |
|---|---|---|---|
| 1 | poly `/book` | `venue_unavailable` (routed to gamma rest, not CLOB) | pytheum-core CLOB book + token_id resolution |
| 2 | `/oi` | null both venues (field exists, not mapped) | pytheum-core Kalshi `get_market` mapping + poly OI query |
| 3 | `/holders` | rows present, all fields null | `normalize_pm_holders` key mapping |
| 4 | `whale-trades` | `market`+`wallet` null | poly trades normalizer / conditionId→market + wallet join |

All four are Polymarket-side data-layer issues in `pytheum-core` (Kalshi `/book`
and the rest of the serve tier work). They surface through the `pytheum` serve
handlers but the fixes are in the venue clients / normalizers. Repro any time
against a live-pointed server; happy to pair on the CLOB book path (#1) since
it's the most-impactful (Polymarket is the more liquid venue).
