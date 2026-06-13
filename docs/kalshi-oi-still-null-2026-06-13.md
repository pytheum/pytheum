# Follow-up: Kalshi `/oi` still returns null (after a123985)

*Verified against `a123985` on 2026-06-13. The other 3 trader-endpoint bugs (poly `/book`, `/holders`, `whale-trades`) are confirmed fixed. This is the one residual.*

## Symptom

`GET /v1/markets/{kalshi_ref}/oi` returns `open_interest: null` for **every** Kalshi
market. Polymarket OI now works post-fix; Kalshi does not.

```
$ curl -s $BASE/v1/markets/kalshi:KXNBA-26-NYK/oi
{"open_interest": null, "venue": "kalshi", "ref": "kalshi:KXNBA-26-NYK", "source": "live"}
$ curl -s $BASE/v1/markets/kalshi:KXMENWORLDCUP-26-PT/oi
{"open_interest": null, ...}
$ curl -s $BASE/v1/markets/kalshi:KXNBA-26-SAS/oi
{"open_interest": null, ...}
```

3/3 markets null — systematic, not market-specific.

## Root cause (confirmed against the live Kalshi API)

`a123985` fixed `normalize_pm_oi` — that's the **Polymarket** OI normalizer only. The
Kalshi side was untouched, and the Kalshi extraction reads the wrong field.

Kalshi returns `open_interest: null` but carries the real value in **`open_interest_fp`**
(a fixed-point **string**). Same on both the list and the single-market detail endpoints:

```
$ curl -s "https://api.elections.kalshi.com/trade-api/v2/markets?tickers=KXNBA-26-NYK"
  open_interest:    None
  open_interest_fp: "27903440.70"   <-- the actual value lives here

$ curl -s "https://api.elections.kalshi.com/trade-api/v2/markets/KXNBA-26-NYK"
  open_interest:    None
  open_interest_fp: "27903440.70"   <-- detail endpoint identical
```

So the Kalshi OI extraction needs to fall back to `open_interest_fp` (parse string →
number) when `open_interest` is null. **Units need confirming** — `_fp` is fixed-point;
verify whether it's contracts or needs scaling (the analogous `volume_fp`/`*_dollars`
split is the same gotcha we hit on the price-sync; the bare `open_interest` field being
null is the same "Kalshi list endpoint nulls the plain quote fields" pattern).

## Fix location

`pytheum-core` SDK — the Kalshi market normalizer / venue client, **not** the
`pytheum/api/markets_oi.py` serve handler (that handler just surfaces whatever the SDK
returns; it's correct). Parallel to the `normalize_pm_oi` you just added, but on the
Kalshi path, reading `open_interest_fp`.
