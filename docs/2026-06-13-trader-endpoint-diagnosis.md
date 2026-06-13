# Trader Endpoint Diagnosis — 2026-06-13

All four broken endpoints were traced to **our serving layer** (`trader/normalizers.py`
and `trader/resolve.py`). The cofounder's core SDK (`pytheum-core`) builds the correct
URLs and returns the raw venue payload without mutation — the breakage happens when we
consume it. No core edits are required.

---

## Reference market used for live probing

| Field | Value |
|---|---|
| Slug | `new-rhianna-album-before-gta-vi-926` |
| Gamma market id | `540817` |
| condition\_id | `0x1fad72fae204143ff1c3035e99e7c0f65ea8d5cd9bd1070987bd1a3316f772be` |
| YES token\_id (clobTokenIds[0]) | `98022490269692409998126496127597032490334070080325855126491859374983463996227` |

---

## Endpoint 1 — open\_interest (`/v1/markets/{ref}/oi`)

### (a) Core method

`PolymarketDataRest.get_open_interest(markets: list[str])`
(`pytheum-core/src/pytheum_core/venues/polymarket/data.py:78-86`)

Builds `GET https://data-api.polymarket.com/oi?market=<condition_id>` (CSV-serialised
under the singular key `market` per `QueryArraySerialization.CSV`).
Returns `list(body)` — the raw JSON list decoded from the response.

### (b) Live venue response

```
GET https://data-api.polymarket.com/oi?market=0x1fad72...

[{"market": "0x1fad72fae204143ff1c3035e99e7c0f65ea8d5cd9bd1070987bd1a3316f772be",
  "value": 21870.138673}]
```

The venue returns the OI quantity under the key **`"value"`**.

### (c) Core output

`data.get_open_interest([condition_id])` returns the list above unchanged:
`[{"market": "0x...", "value": 21870.138673}]`. Core is correct.

### (d) Our normalizer output

`normalize_pm_oi` (`normalizers.py:248-265`) iterates the list and looks up:
```python
oi = _safe_float(item.get("open_interest_count") or item.get("open_interest"))
```
Neither key exists in the response. `oi` is `None` for every item → `total` stays
`None` → endpoint returns `{"open_interest": null, ...}`.

### (e) Verdict

| Layer | File:line | Discrepancy |
|---|---|---|
| **OUR normalizer** | `trader/normalizers.py:257` | Checks `open_interest_count` / `open_interest`; venue returns `"value"` |

**Fix (ours):** In `normalize_pm_oi`, extend the field-lookup chain:

```python
# normalizers.py:257  (single-line change)
oi = _safe_float(item.get("open_interest_count") or item.get("open_interest") or item.get("value"))
```

---

## Endpoint 2 — market\_holders (`/v1/markets/{ref}/holders`)

### (a) Core method

`PolymarketDataRest.get_holders(market: str)`
(`pytheum-core/src/pytheum_core/venues/polymarket/data.py:115-126`)

Builds `GET https://data-api.polymarket.com/holders?market=<condition_id>`.
Returns `list(body)`.

### (b) Live venue response

```
GET https://data-api.polymarket.com/holders?market=0x1fad72...

[
  {
    "token": "98022490...",     ← YES token_id
    "holders": [
      {"proxyWallet": "0xdf6d...", "amount": 4532.3, "asset": "98022490...",
       "outcomeIndex": 1, "name": "TheRedChip", "pseudonym": "Willing-Minnow", ...},
      ...
    ]
  },
  {
    "token": "53831553...",     ← NO token_id
    "holders": [...]
  }
]
```

The API returns a **two-level nested structure**: an outer list of
`{"token": <token_id>, "holders": [...per-holder-dicts...]}` objects.
The actual holder fields (`proxyWallet`, `amount`, etc.) live inside the inner list.

### (c) Core output

`data.get_holders(market=condition_id)` returns the outer list unchanged. Core is
correct — it faithfully passes back what the venue sent.

### (d) Our normalizer output

`normalize_pm_holders` (`normalizers.py:301-322`) iterates the outer list and for
each `item` does:
```python
addr_raw = item.get("address") if item.get("address") is not None else item.get("proxyWallet")
```
But each outer `item` has only keys `"token"` and `"holders"` — not `"address"`,
`"proxyWallet"`, `"amount"`, or `"outcome"`. All three fields resolve to `None`.
The endpoint returns a `holders` list filled with all-null entries.

### (e) Verdict

| Layer | File:line | Discrepancy |
|---|---|---|
| **OUR normalizer** | `trader/normalizers.py:301-322` | Iterates the outer wrapper dicts, not the inner holder records; venue nesting is `[{token, holders:[...]}]` not `[{proxyWallet, amount, ...}]` |

**Fix (ours):** In `normalize_pm_holders`, unwrap the two levels:

```python
def normalize_pm_holders(items: list[dict[str, Any]], *, ref: str) -> dict[str, Any]:
    holders: list[dict[str, Any]] = []
    for outer in items:
        # Venue returns {token: <token_id>, holders: [...per-holder...]}
        token_id = outer.get("token")
        for item in (outer.get("holders") or []):
            addr_raw = item.get("proxyWallet") or item.get("address")
            holders.append({
                "address": addr_raw,
                "amount": _safe_float(item.get("amount") or item.get("size")),
                "outcome": (
                    item.get("outcome")
                    or item.get("asset")
                    or item.get("asset_id")
                    or token_id
                ),
            })
    return {
        "holders": holders,
        "count": len(holders),
        "ref": ref,
        "source": "live",
        "venue": "polymarket",
        "note": _PM_ONLY_NOTE,
    }
```

---

## Endpoint 3 — Polymarket CLOB orderbook (`/v1/markets/{ref}/book`)

### (a) Core method

`PolymarketClobRest.get_book(token_id: str)`
(`pytheum-core/src/pytheum_core/venues/polymarket/clob.py:53-59`)

Builds `GET https://clob.polymarket.com/book?token_id=<token_id>`.

The `token_id` is resolved beforehand by our `trader/resolve.py:resolve_pm` →
`_extract_resolved`, which reads `clobTokenIds` from the Gamma market dict.

### (b) Live venue response (CLOB, correct token\_id)

```
GET https://clob.polymarket.com/book?token_id=98022490...

{
  "market": "0x1fad72...",
  "asset_id": "98022490...",
  "timestamp": "1781379678568",
  "bids": [{"price": "0.01", "size": "255.59"}, ...],
  "asks": [...]
}
```

The CLOB `/book` endpoint is healthy when called with the correct token\_id. The
response has `bids`/`asks` with `{"price": str, "size": str}` dicts — exactly what
`normalize_pm_book` expects.

**Gamma `clobTokenIds` field type — the root cause:**

```
GET https://gamma-api.polymarket.com/markets?condition_ids=0x1fad72...&limit=1

→ m["clobTokenIds"] is a JSON-encoded STRING, not a Python list:
  '["98022490...", "53831553..."]'
```

Confirmed by type inspection:
```python
type(m["clobTokenIds"])  # → <class 'str'>
```

### (c) What our resolver produces

`trader/resolve.py:_extract_resolved` (`resolve.py:54-69`):

```python
clob_ids: list[str] | None = market.get("clobTokenIds")
# clob_ids is the STRING '["98022490...","53831553..."]'
if not clob_ids:          # non-empty string → truthy → no raise
    raise ValueError(...)
token_id = str(clob_ids[0])  # clob_ids[0] on a string → '[' (first char)
# token_id = '['
```

`pm_client.clob.get_book("[")` hits `GET /book?token_id=%5B` which returns:

```json
HTTP 404  {"error": "No orderbook exists for the requested token id"}
```

This surfaces as a `VenueUnavailable` exception (caught by the handler) and the
endpoint emits `{"error": "venue_unavailable", ...}`.

### (d) Normalizer

The normalizer is never reached because the CLOB call 404s before returning data.
If given correct data, `normalize_pm_book` would work correctly.

### (e) Verdict

| Layer | File:line | Discrepancy |
|---|---|---|
| **OUR resolve.py** | `trader/resolve.py:54-69` (`_extract_resolved`) | `clobTokenIds` from Gamma is a JSON-encoded string; code indexes into it as a list, getting `'['` as the token\_id |

**Fix (ours):** In `trader/resolve.py:_extract_resolved`, decode the string before
indexing:

```python
import json as _json

def _extract_resolved(market: dict[str, Any]) -> PmResolved:
    clob_ids: Any = market.get("clobTokenIds")
    # Gamma returns clobTokenIds as a JSON-encoded string, not a Python list.
    if isinstance(clob_ids, str):
        try:
            clob_ids = _json.loads(clob_ids)
        except (ValueError, _json.JSONDecodeError):
            clob_ids = None
    if not clob_ids:
        raise ValueError(
            f"Gamma market has no clobTokenIds — cannot resolve to CLOB token_id. "
            f"Market: {market.get('id')!r}"
        )
    token_id = str(clob_ids[0])
    condition_id = str(market.get("conditionId") or "")
    if not condition_id:
        raise ValueError(
            f"Gamma market has no conditionId — cannot resolve for Data API. "
            f"Market: {market.get('id')!r}"
        )
    return PmResolved(token_id=token_id, condition_id=condition_id)
```

Note: `conditionId` from Gamma IS a plain Python string (`str`), not JSON-encoded —
confirmed live — so no analogous fix is needed for the OI path.

---

## Endpoint 4 — whale\_trades market + wallet joins (`/v1/markets/whale-trades`)

### (a) Core method

`PolymarketDataRest.get_trades(markets: list[str] | None, limit: int, ...)`
(`pytheum-core/src/pytheum_core/venues/polymarket/data.py:52-76`)

Builds `GET https://data-api.polymarket.com/trades?market=<condition_id>&limit=N`
(CSV-encoded under `market`). Returns `list(body)`.

### (b) Live venue response

```
GET https://data-api.polymarket.com/trades?market=0x1fad72...&limit=2

[
  {
    "proxyWallet": "0x9e031b2be87b9f5582c6988aa5a38455cc666bbe",
    "side": "SELL",
    "asset": "98022490...",            ← token_id (not "asset_id")
    "conditionId": "0x1fad72...",      ← market condition_id (not "market")
    "size": 36.88,
    "price": 0.5,
    "timestamp": 1781372776,
    "outcome": "Yes",
    "name": "", "pseudonym": "",
    ...
  },
  ...
]
```

Key observations:
- Market identifier is `"conditionId"`, not `"market"` or `"asset_id"`.
- Token identifier is `"asset"`, not `"asset_id"`.
- Trader wallet is `"proxyWallet"`, not `"maker"`, `"taker"`, or `"trader"`.
- `size` and `price` are JSON numbers, not strings — `_safe_float` handles them correctly.
- `timestamp` is seconds-epoch integer (1781372776) — existing ms-detection branch
  (`ts_raw > 1e12`) correctly leaves it as-is.

### (c) Core output

`data.get_trades(markets=[condition_id], limit=...)` returns the list above unchanged.
Filtering by `market=condition_id` query param works correctly — the venue accepts it
and returns only trades for that market. Core is correct.

### (d) Our normalizer output

`normalize_pm_whale_trades` (`normalizers.py:389-431`):

```python
"market": item.get("market") or item.get("asset_id"),   # → None (neither key exists)
"wallet": item.get("maker") or item.get("taker") or item.get("trader"),  # → None
"pseudonym": item.get("pseudonym") or item.get("name"),  # → None (both empty strings)
```

`market` and `wallet` are always `None`. Trades pass the `notional_usd >= min_usd`
filter correctly (price × size resolves to a real float), so results are emitted —
but every trade has `market: null, wallet: null`, making the join-level data useless.

### (e) Verdict

| Layer | File:line | Discrepancy |
|---|---|---|
| **OUR normalizer** | `trader/normalizers.py:420` | Checks `"market"` / `"asset_id"` for the condition\_id; venue sends `"conditionId"` |
| **OUR normalizer** | `trader/normalizers.py:426` | Checks `"maker"` / `"taker"` / `"trader"` for wallet; venue sends `"proxyWallet"` |

**Fix (ours):** In `normalize_pm_whale_trades`, update the two field-lookup lines:

```python
# normalizers.py:420
"market": item.get("market") or item.get("conditionId") or item.get("asset_id") or item.get("asset"),

# normalizers.py:426
"wallet": item.get("proxyWallet") or item.get("maker") or item.get("taker") or item.get("trader"),
```

---

## Summary verdict table

| # | Endpoint | Layer broken | Exact file:line | Discrepancy | Fix owner |
|---|---|---|---|---|---|
| 1 | `GET /oi` | Our normalizer | `trader/normalizers.py:257` | Venue returns `"value"`, normalizer checks `"open_interest_count"` / `"open_interest"` | **US** |
| 2 | `GET /holders` | Our normalizer | `trader/normalizers.py:301-322` | Venue returns `[{token, holders:[...]}]` two-level nesting; normalizer iterates the outer wrapper dicts, misses inner records | **US** |
| 3 | `GET /book` | Our resolve.py | `trader/resolve.py:54-69` (`_extract_resolved`) | Gamma `clobTokenIds` is a JSON-encoded string; indexing with `[0]` yields `'['` (first char), not the token\_id integer | **US** |
| 4 | `GET /whale-trades` | Our normalizer | `trader/normalizers.py:420,426` | Venue sends `"conditionId"` (not `"market"`/`"asset_id"`) and `"proxyWallet"` (not `"maker"`/`"taker"`/`"trader"`) | **US** |

## Cofounder / core handoff

**No core edits required.** All four failures occur after core correctly fetches and
returns the raw venue payload:

- `PolymarketDataRest.get_open_interest` → URL and serialization correct. ✓
- `PolymarketDataRest.get_holders` → URL correct, nested payload returned faithfully. ✓
- `PolymarketClobRest.get_book` → method + URL correct. ✓
- `PolymarketDataRest.get_trades` → URL, CSV param, and market filter correct. ✓

The only thing worth noting to the cofounder (not a bug, just a documentation gap):
the Gamma API returns `clobTokenIds` as a **JSON-encoded string**, not a native JSON
array. This is a Polymarket quirk that any consumer of the Gamma market dict must
handle. It may be worth adding a note or a helper in core's gamma normalizer so future
consumers don't hit the same trap.

## ID-type map (Polymarket)

| ID type | Format | Used by |
|---|---|---|
| `condition_id` | `0x<64 hex>` | Data API `/oi?market=`, `/holders?market=`, `/trades?market=`; CLOB `/data/order?market=` |
| `token_id` | 20+ digit integer (string) | CLOB `/book?token_id=`, `/spread?token_id=`, etc. |
| Gamma numeric market id | plain integer string | Gamma `/markets/{id}` |
| Gamma slug | kebab-case string | Gamma `/markets/slug/{slug}` |

Our `resolve_pm` correctly routes each ref format to the appropriate Gamma call and
extracts both IDs. The only gap was the string-decode of `clobTokenIds` (endpoint 3).
