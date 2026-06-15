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
import json
from datetime import datetime
from typing import Any

import asyncpg
import httpx

from scripts.load_market_equivalence import _db_url

_GAMMA = "https://gamma-api.polymarket.com/markets/"
_CONCURRENCY = 4

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


async def run(*, limit: int | None, refresh: bool) -> None:
    con = await asyncpg.connect(_db_url(), statement_cache_size=0)
    try:
        await con.execute("SET statement_timeout = 0")
        q = _REFRESH_QUERY if refresh else _MISSING_QUERY
        if limit:
            q += f" LIMIT {int(limit)}"
        ids = [r["polymarket_market_id"] for r in await con.fetch(q)]
        print(f"{'refreshing' if refresh else 'filling'} {len(ids)} poly rows from Gamma")

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
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--refresh", action="store_true",
                    help="re-pull quotes for previously supplemental rows")
    args = ap.parse_args()
    asyncio.run(run(limit=args.limit, refresh=args.refresh))


def _main_guard() -> None:  # pragma: no cover
    main()


if __name__ == "__main__":
    main()
