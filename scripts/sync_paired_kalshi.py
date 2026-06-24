"""Upsert missing KALSHI rows for equivalence pairs into the serving markets table.

The companion ``sync_paired_polymarket`` fills missing *Polymarket* rows but only for
pairs whose KALSHI leg is already present (it JOINs ``markets ka``). Freshly-wired
structured clusters (e.g. the tennis-total ``KXATPGTOTAL…`` series) have the *Kalshi*
leg missing, so those pairs drop as missing-leg in browse/divergences and never surface
(ali's eval finding #2). This script closes that gap.

Source of truth for the missing Kalshi identity rows is the matcher's local market DB
(``--source-db``, a SQLite ``markets`` table) — it already fetched every market it
matched, so no Kalshi API auth/rate-limit is needed for a laptop-side run. We write only
the identity + an initial book; the serving price-refresh sidecar keeps ``bestBid``/
``bestAsk`` fresh once the row exists.

Reversible: every inserted row carries ``payload.synced_by = 'kalshi_supplemental'``.
DRY-RUN by default — pass ``--write`` to actually upsert.

Usage:
    python -m scripts.sync_paired_kalshi --source-db /path/to/matcher/data/markets.db
    python -m scripts.sync_paired_kalshi --source-db … --write
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import datetime as _dt
import gzip
import json
import sqlite3
from datetime import datetime
from typing import Any

# asyncpg + _db_url are imported lazily inside run() — they're runtime-only deps
# (not in the dev venv), so keeping them out of module import makes the pure row
# mapper importable + unit-testable (mirrors why the sibling scripts have no tests).

_MISSING_QUERY = """
SELECT DISTINCT e.kalshi_market_id
FROM market_equivalence e
LEFT JOIN markets ka ON ka.id = e.kalshi_market_id
WHERE e.kalshi_market_id IS NOT NULL AND ka.id IS NULL
"""

_UPSERT = """
INSERT INTO markets (id, title, venue, status, volume_usd, liquidity_usd, url,
                     resolution_at, payload)
VALUES ($1, $2, 'kalshi', $3, $4, $5, $6, $7, $8)
ON CONFLICT (id) DO UPDATE SET
    status = EXCLUDED.status,
    volume_usd = EXCLUDED.volume_usd,
    liquidity_usd = EXCLUDED.liquidity_usd,
    resolution_at = EXCLUDED.resolution_at,
    payload = EXCLUDED.payload
"""


def _num(v: Any) -> float | None:
    try:
        return float(v) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _iso(value: Any) -> datetime | None:
    if not value:
        return None
    with contextlib.suppress(ValueError, TypeError):
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return None


def kalshi_row_to_market(market_id: str, title: str | None, status: str | None,
                         close_date: str | None, raw_json: str | None) -> tuple[Any, ...] | None:
    """Map a matcher-DB Kalshi row to a serving markets-table row tuple.

    market_id is the bare ticker (e.g. KXATPGTOTAL-…); the serving id is kalshi:<ticker>.
    Book fields (bestBid/bestAsk from Kalshi cents) are initial values; the serving
    price-refresh sidecar updates them once the row exists.
    """
    if not market_id:
        return None
    d: dict[str, Any] = {}
    if raw_json:
        with contextlib.suppress(json.JSONDecodeError, TypeError):
            d = json.loads(raw_json)
    # Kalshi quotes YES in integer cents (0–100); the serving book wants [0,1] floats.
    yes_bid, yes_ask = _num(d.get("yes_bid")), _num(d.get("yes_ask"))
    last = _num(d.get("last_price"))
    payload: dict[str, Any] = {
        "bestBid": yes_bid / 100.0 if yes_bid is not None else None,
        "bestAsk": yes_ask / 100.0 if yes_ask is not None else None,
        "lastTradePrice": last / 100.0 if last is not None else None,
        "rules_primary": (d.get("rules_primary") or "")[:1000] or None,
        "event_ticker": d.get("event_ticker"),
        "synced_by": "kalshi_supplemental",
    }
    payload = {k: v for k, v in payload.items() if v is not None}
    serving_status = "active" if (status == "active") else "closed"
    event_ticker = d.get("event_ticker")
    url = f"https://kalshi.com/markets/{event_ticker}" if event_ticker else None
    resolution_at = _iso(close_date) or _iso(d.get("close_time")) or _iso(d.get("expiration_time"))
    return (
        f"kalshi:{market_id}",
        title or d.get("title"),
        serving_status,
        _num(d.get("volume_fp") or d.get("volume")),
        _num(d.get("liquidity_dollars")),
        url,
        resolution_at,
        json.dumps(payload),
    )


def _load_source_rows(source_db: str, tickers: set[str],
                      min_date: str | None) -> dict[str, tuple[Any, ...]]:
    """Return {serving_id: market_row_tuple} for the requested tickers found in the
    matcher SQLite DB. serving_id = kalshi:<ticker>. ``min_date`` skips rows whose
    close_date is before it (live-only scope)."""
    con = sqlite3.connect(source_db)
    out: dict[str, tuple[Any, ...]] = {}
    bare = {t.split(":", 1)[1] if t.startswith("kalshi:") else t for t in tickers}
    q = ("SELECT market_id, title, status, close_date, raw_json FROM markets "
         "WHERE platform='kalshi' AND market_id = ?")
    for tk in bare:
        r = con.execute(q, (tk,)).fetchone()
        if r is None:
            continue
        if min_date and (r[3] or "")[:10] < min_date:  # r[3] = close_date
            continue
        row = kalshi_row_to_market(*r)
        if row is not None:
            out[row[0]] = row
    con.close()
    return out


def _effective_date(game_date: str | None, resolution_date: str | None) -> str | None:
    """The pair's true event/liveness date. For sports, game_date is authoritative:
    Kalshi's close_time can lag the actual match by weeks (KXATPGTOTAL-26JUN22 has
    game_date 2026-06-22 but Kalshi close 2026-07-06), so a past game looks 'live' by
    resolution_date and gets paired with an already-closed/unquotable PM leg. Prefer
    game_date when present, else fall back to resolution_date (events have no game_date)."""
    return game_date or resolution_date


def export_row_to_market(kalshi_ref: str | None, kalshi_title: str | None,
                         game_date: str | None, resolution_date: str | None,
                         today: str) -> tuple[Any, ...] | None:
    """Build a minimal serving markets row from an equivalence-export row.

    Box-side source — needs no matcher DB or Kalshi API. Identity only: the box
    price-refresh sidecar + market_metadata poll fill the book and the venue-precise
    resolution_at once the row exists. The effective event date (game_date else
    resolution_date) seeds resolution_at + status (the box's sweep_settled reconciles).
    """
    if not kalshi_ref or not kalshi_ref.startswith("kalshi:"):
        return None
    eff = _effective_date(game_date, resolution_date)
    d = (eff or "")[:10]
    status = "closed" if (d and d < today) else "active"
    payload = {"synced_by": "kalshi_supplemental", "source": "export"}
    return (
        kalshi_ref, kalshi_title, status, None, None, None,
        _iso(eff), json.dumps(payload),
    )


def _load_export_rows(export_path: str, ids: set[str], today: str,
                      min_date: str | None) -> dict[str, tuple[Any, ...]]:
    """Return {serving_id: row} for the missing ids found in the equivalence export.

    When ``min_date`` is set, rows whose EFFECTIVE date (game_date else resolution_date)
    is before it — or that have no date at all — are skipped. Filtering on game_date is
    the fix for the past-game one-sided-pair regression (Kalshi's close_time lags the
    event, so resolution_date alone admitted resolved matches with closed PM legs).
    """
    out: dict[str, tuple[Any, ...]] = {}
    want = set(ids)
    with gzip.open(export_path, "rt", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            r = json.loads(line)
            ref = r.get("kalshi_ref")
            if ref not in want:
                continue
            if min_date:
                eff = (_effective_date(r.get("game_date"), r.get("resolution_date")) or "")[:10]
                if not eff or eff < min_date:  # undated or past-event → skip in live-only
                    continue
            row = export_row_to_market(ref, r.get("kalshi_title"), r.get("game_date"),
                                       r.get("resolution_date"), today)
            if row is not None:
                out[ref] = row
    return out


async def run(*, source_db: str | None, from_export: str | None, today: str,
              min_resolution_date: str | None, write: bool, limit: int | None) -> None:
    import asyncpg

    from scripts.load_market_equivalence import _db_url
    con = await asyncpg.connect(_db_url(), statement_cache_size=0)
    try:
        await con.execute("SET statement_timeout = 0")
        q = _MISSING_QUERY + (f" LIMIT {int(limit)}" if limit else "")
        missing = [r["kalshi_market_id"] for r in await con.fetch(q)]
        scope = (f"live-only (effective date [game_date else resolution_date] >= "
                 f"{min_resolution_date})" if min_resolution_date
                 else "ALL (incl. historical/closed)")
        print(f"missing Kalshi legs in serving markets table: {len(missing)}  | scope: {scope}")
        print("[sync_paired_kalshi @ game_date-aware filter]")  # build-marker: confirms #26+ is running
        if not missing:
            print("nothing to do — every equivalence pair's Kalshi leg is present.")
            return

        if from_export:
            rows_by_id = _load_export_rows(from_export, set(missing), today, min_resolution_date)
            src_label = "export"
        else:
            assert source_db is not None  # main() requires exactly one source
            rows_by_id = _load_source_rows(source_db, set(missing), min_resolution_date)
            src_label = "matcher DB"
        found = [rows_by_id[m] for m in missing if m in rows_by_id]
        skipped = len(missing) - len(found)
        print(f"in-scope rows from {src_label}: {len(found)}  | "
              f"out-of-scope/not-in-source (skipped): {skipped}")
        for sample in found[:5]:
            print(f"  would-upsert: {sample[0]}  status={sample[2]}  "
                  f"resolves={sample[6]}  title={str(sample[1])[:40]!r}")

        if not write:
            print(f"\nDRY-RUN: would upsert {len(found)} Kalshi rows ({scope}). "
                  f"Re-run with --write to apply.")
            return

        await con.executemany(_UPSERT, found)
        n_live = await con.fetchval(
            "SELECT count(*) FROM market_equivalence e "
            "JOIN markets ka ON ka.id = e.kalshi_market_id AND ka.status='active' "
            "JOIN markets pa ON pa.id = e.polymarket_market_id AND pa.status='active'"
        )
        print(f"\nUPSERTED {len(found)} Kalshi rows. both-legs-active pairs now: {n_live}")
    finally:
        await con.close()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--from-export",
                     help="equivalence-export.jsonl.gz — box-side source, no matcher DB "
                          "(identity rows; box sidecar fills book/precise resolution).")
    src.add_argument("--source-db",
                     help="matcher SQLite markets DB (laptop-side source; richer rows).")
    ap.add_argument("--today", default=_dt.date.today().isoformat(),
                    help="Reference date for active/closed status from resolution_date.")
    ap.add_argument("--live-only", action="store_true",
                    help="Only upsert legs whose resolution_date >= --today (the live "
                         "coverage lift; ~97%% of missing legs are historical/closed and "
                         "pointless to backfill). Shorthand for --min-resolution-date=today.")
    ap.add_argument("--min-resolution-date", default=None,
                    help="Only upsert legs resolving on/after this YYYY-MM-DD.")
    ap.add_argument("--write", action="store_true",
                    help="Actually upsert. Omitted = DRY-RUN (count + sample only).")
    ap.add_argument("--limit", type=int, default=None)
    args = ap.parse_args()
    min_date = args.min_resolution_date or (args.today if args.live_only else None)
    asyncio.run(run(source_db=args.source_db, from_export=args.from_export,
                    today=args.today, min_resolution_date=min_date,
                    write=args.write, limit=args.limit))


if __name__ == "__main__":
    main()
