"""Upsert missing Polymarket rows for equivalence pairs, straight from Gamma.

The matcher's pairs resolve to poly market ids (scripts/resolve_polymarket_slugs)
that our metadata poll doesn't always carry — game/tennis/esports markets churn
daily. Gamma is public, so we fill those rows ourselves: same columns + payload
keys the stream readers use (bestBid/bestAsk/outcomePrices/conditionId/...),
plus `outcomes` (names) which the side-mapper needs, and a `synced_by` marker
for provenance.

Default scope: resolved pairs whose KALSHI leg is in our markets table but
whose poly row is missing. Re-runnable; ON CONFLICT updates quotes/status.

Usage:
    python -m scripts.sync_paired_polymarket [--limit N] [--refresh]
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import datetime as _dt
import gzip
import json
from datetime import datetime
from typing import Any

import asyncpg
import httpx

from scripts.load_market_equivalence import _db_url

_GAMMA = "https://gamma-api.polymarket.com/markets/"
_CONCURRENCY = 4


def _effective_date(game_date: str | None, resolution_date: str | None) -> str | None:
    """The pair's true liveness date — game_date is authoritative for sports (Kalshi's
    close lags the event); fall back to resolution_date for events. Mirrors sync_paired_kalshi."""
    return game_date or resolution_date


def _live_pm_refs(export_path: str, min_date: str | None) -> set[str]:
    """pm_refs (polymarket:<gamma>) from the export; if min_date is set, only pairs whose
    effective date (game_date else resolution_date) is on/after it.

    These gamma ids are the EXPORT's own — present for the recent front that the table's
    slug-resolution can't reach — so the backfill uses them directly (export-driven),
    decoupled from the stale market_equivalence table. min_date=None returns every pm_ref."""
    refs: set[str] = set()
    with gzip.open(export_path, "rt", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            r = json.loads(line)
            ref = r.get("pm_ref")
            if not ref:
                continue
            if min_date is not None:
                eff = (_effective_date(r.get("game_date"), r.get("resolution_date")) or "")[:10]
                if not eff or eff < min_date:
                    continue
            refs.add(ref)
    return refs

_MISSING_QUERY = """
SELECT DISTINCT e.polymarket_market_id
FROM market_equivalence e
JOIN markets ka ON ka.id = e.kalshi_market_id
LEFT JOIN markets pa ON pa.id = e.polymarket_market_id
WHERE e.polymarket_market_id IS NOT NULL AND pa.id IS NULL
"""

_REFRESH_QUERY = """
SELECT DISTINCT e.polymarket_market_id
FROM market_equivalence e
JOIN markets ka ON ka.id = e.kalshi_market_id AND ka.status = 'active'
JOIN markets pa ON pa.id = e.polymarket_market_id
WHERE pa.payload::jsonb ->> 'synced_by' = 'equivalence_supplemental'
"""

_UPSERT = """
INSERT INTO markets (id, title, venue, status, volume_usd, liquidity_usd, url,
                     resolution_at, payload)
VALUES ($1, $2, 'polymarket', $3, $4, $5, $6, $7, $8)
ON CONFLICT (id) DO UPDATE SET
    status = EXCLUDED.status,
    volume_usd = EXCLUDED.volume_usd,
    liquidity_usd = EXCLUDED.liquidity_usd,
    resolution_at = EXCLUDED.resolution_at,
    payload = EXCLUDED.payload
"""


def market_to_row(m: dict[str, Any]) -> tuple | None:
    """Map one Gamma market object to a markets-table row tuple."""
    mid = m.get("id")
    if mid is None:
        return None
    status = "active" if (m.get("active") and not m.get("closed")) else "closed"
    end = m.get("endDate") or m.get("endDateIso")
    resolution_at = None
    if end:
        with contextlib.suppress(ValueError):
            resolution_at = datetime.fromisoformat(str(end).replace("Z", "+00:00"))
    events = m.get("events") or []
    ev_slug = (events[0].get("slug") if events and isinstance(events[0], dict)
               else None) or m.get("slug")
    payload = {
        "bestBid": m.get("bestBid"),
        "bestAsk": m.get("bestAsk"),
        "lastTradePrice": m.get("lastTradePrice"),
        "oneDayPriceChange": m.get("oneDayPriceChange"),
        "spread": m.get("spread"),
        "outcomePrices": m.get("outcomePrices"),
        "outcomes": m.get("outcomes"),
        "conditionId": m.get("conditionId"),
        "description": (m.get("description") or "")[:500] or None,
        "group_item_title": m.get("groupItemTitle"),
        "synced_by": "equivalence_supplemental",
    }
    return (
        f"polymarket:{mid}",
        m.get("question"),
        status,
        _num(m.get("volumeNum") or m.get("volume")),
        _num(m.get("liquidityNum") or m.get("liquidity")),
        f"https://polymarket.com/event/{ev_slug}" if ev_slug else None,
        resolution_at,
        json.dumps(payload),
    )


def _num(v: Any) -> float | None:
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


async def run(*, limit: int | None, refresh: bool, from_export: str | None,
              min_resolution_date: str | None, write: bool) -> None:
    con = await asyncpg.connect(_db_url(), statement_cache_size=0)
    try:
        await con.execute("SET statement_timeout = 0")
        if from_export and not refresh:
            # Export-driven missing-PM determination (decoupled from the stale
            # market_equivalence table AND its markets.url slug resolution, which lands NULL
            # for exactly the recent front we need — chicken-and-egg). The export's pm_ref is
            # the gamma id directly (present →2.63M front), so: take the export's (live) PM
            # legs, and the missing set is the ones absent from `markets`.
            want = _live_pm_refs(from_export, min_resolution_date)
            present = {r["id"] for r in await con.fetch(
                "SELECT id FROM markets WHERE id = ANY($1::text[])", list(want))}
            ids = [r for r in want if r not in present]
            scope = (f"export-driven{' live-only' if min_resolution_date else ''}: "
                     f"{len(ids)} missing-PM of {len(want)} export PM legs absent from markets")
        else:
            q = _REFRESH_QUERY if refresh else _MISSING_QUERY
            ids = [r["polymarket_market_id"] for r in await con.fetch(q)]
            scope = "table-driven refresh" if refresh else "table-driven (market_equivalence)"
        if limit:
            ids = ids[:int(limit)]

        print(f"{'refreshing' if refresh else 'filling'} {len(ids)} poly rows from Gamma  | scope: {scope}")
        for sample in ids[:5]:
            print(f"  would-fill: {sample}")
        if not write:
            print(f"\nDRY-RUN: would fetch + upsert up to {len(ids)} Polymarket legs from Gamma "
                  f"(concurrent). Re-run with --write to apply.")
            return

        sem = asyncio.Semaphore(_CONCURRENCY)
        rows: list[tuple] = []
        misses = 0

        async with httpx.AsyncClient() as client:
            async def one(pid: str) -> None:
                nonlocal misses
                raw = pid.split(":", 1)[1]
                async with sem:
                    try:
                        r = await client.get(_GAMMA + raw, timeout=15)
                        r.raise_for_status()
                        row = market_to_row(r.json())
                    except (httpx.HTTPError, ValueError):
                        row = None
                    await asyncio.sleep(0.1)
                if row is None:
                    misses += 1
                else:
                    rows.append(row)

            for i in range(0, len(ids), 500):
                await asyncio.gather(*(one(p) for p in ids[i:i + 500]))
                print(f"  {min(i + 500, len(ids))}/{len(ids)} "
                      f"(rows={len(rows)} misses={misses})")

        if rows:
            await con.executemany(_UPSERT, rows)
        n_live = await con.fetchval(
            "SELECT count(*) FROM market_equivalence e "
            "JOIN markets ka ON ka.id = e.kalshi_market_id AND ka.status='active' "
            "JOIN markets pa ON pa.id = e.polymarket_market_id AND pa.status='active'"
        )
        print(f"upserted={len(rows)} misses={misses} | both-legs-active pairs now: {n_live}")
    finally:
        await con.close()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--refresh", action="store_true",
                    help="re-pull quotes for previously supplemental rows")
    ap.add_argument("--from-export",
                    help="equivalence-export.jsonl.gz — used to scope --live-only to live pairs.")
    ap.add_argument("--today", default=_dt.date.today().isoformat(),
                    help="Reference date for --live-only.")
    ap.add_argument("--live-only", action="store_true",
                    help="Only fill PM legs whose pair is live (effective date >= --today). "
                         "Requires --from-export. Avoids fetching ~100k historical legs.")
    ap.add_argument("--min-resolution-date", default=None,
                    help="Only fill legs whose pair's effective date is on/after this YYYY-MM-DD.")
    ap.add_argument("--write", action="store_true",
                    help="Actually fetch+upsert. Omitted = DRY-RUN (scoped count + sample only).")
    args = ap.parse_args()
    min_date = args.min_resolution_date or (args.today if args.live_only else None)
    if args.live_only and not args.from_export:
        ap.error("--live-only requires --from-export (liveness comes from the export rows)")
    asyncio.run(run(limit=args.limit, refresh=args.refresh, from_export=args.from_export,
                    min_resolution_date=min_date, write=args.write))


def _main_guard() -> None:  # pragma: no cover
    main()


if __name__ == "__main__":
    main()
