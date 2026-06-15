"""Flip stuck `status='active'` rows to venue truth (#260 / #213).

Mechanism of the bug: the price sidecars refresh OPEN markets only, so a
market that settles simply stops being updated — its row freezes at
status='active' with a pre-settlement quote (the 2026-06-11 benchmark's worst
trap: a settled-YES contract served at 0.52 "active").

This sweep VERIFIES suspects against the venues' public APIs and writes the
venue's own status — no heuristics, no mass-closing on absence:
  - suspects = active rows whose resolution_at has passed, OR whose price tape
    has gone silent >12h despite a near-dated resolution (catches early
    settlements like KXCLAUDE whose listed end date is months out)
  - Kalshi: GET /trade-api/v2/markets?tickers=... (status: active|finalized|...)
  - Polymarket: GET gamma /markets/{id} (closed / active flags)
Rows the venue still calls open are left untouched (the Fujimori case: listed
end date passed but actively trading — that market stays active).

Runs from the pytheum-equivalence-refresh timer; cap bounds each run.

Usage:
    python -m scripts.sweep_settled_markets [--cap N] [--dry-run]
"""
from __future__ import annotations

import argparse
import asyncio

import asyncpg
import httpx

from scripts.load_market_equivalence import _db_url

_KALSHI = "https://api.elections.kalshi.com/trade-api/v2/markets"
_GAMMA = "https://gamma-api.polymarket.com/markets/"
# Bundle/event PARENT rows carry EVENT ids — /markets/{id} 404s on them, so
# fall back to /events/{id} (same `closed` flag; ignore its quirky
# always-true `active`). This is the #213 parent residue.
_GAMMA_EVENTS = "https://gamma-api.polymarket.com/events/"
_BATCH = 20
_CONCURRENCY = 4
_SILENT_AFTER_H = 12

# Suspects: resolution passed, or (near-dated + tape silent). The tape-silence
# arm only inspects markets that HAVE history (tracked at some point) — rows
# never tracked are judged by resolution_at alone.
_SUSPECTS = f"""
SELECT m.id, m.venue
FROM markets m
WHERE m.status = 'active' AND m.venue = $1
  AND (m.status_checked_at IS NULL OR m.status_checked_at < now() - interval '24 hours')
  AND (
    m.resolution_at < now()
    OR (
      -- Tape silence = the price sidecar stopped refreshing = the venue
      -- dropped the market from its open set. (The sidecar appends NO-CHANGE
      -- observations on every refresh, so a quiet-but-alive market still has
      -- a fresh tape — silence is settlement/delisting, regardless of how far
      -- out the LISTED end date is: early settlements like KXCLAUDE carry
      -- end dates months past their actual resolution.)
      EXISTS (SELECT 1 FROM market_price_history h WHERE h.market_id = m.id)
      AND NOT EXISTS (
        SELECT 1 FROM market_price_history h
        WHERE h.market_id = m.id
          AND h.observed_at > now() - interval '{_SILENT_AFTER_H} hours'
      )
    )
  )
LIMIT $2
"""


def kalshi_status(venue_status: str | None) -> str | None:
    """Map Kalshi's market status to ours; None = leave the row alone."""
    if venue_status in ("active", "open", None):
        return None
    return venue_status  # finalized / settled / closed / determined — venue's word


async def _sweep_kalshi(con: asyncpg.Connection, client: httpx.AsyncClient,
                        ids: list[str], *, dry_run: bool) -> int:
    flipped = 0
    tickers = [i.split(":", 1)[1] for i in ids]
    for i in range(0, len(tickers), _BATCH):
        chunk = tickers[i:i + _BATCH]
        try:
            r = await client.get(_KALSHI, params={"tickers": ",".join(chunk)}, timeout=20)
            r.raise_for_status()
            markets = r.json().get("markets") or []
        except (httpx.HTTPError, ValueError):
            continue
        checked = [(f"kalshi:{m['ticker']}", kalshi_status(m.get("status")))
                   for m in markets if m.get("ticker")]
        updates = [(mid, st) for mid, st in checked if st is not None]
        if not dry_run:
            if updates:
                await con.executemany(
                    "UPDATE markets SET status = $2, status_checked_at = now() "
                    "WHERE id = $1", updates)
            confirmed = [(mid,) for mid, st in checked if st is None]
            if confirmed:
                # Stamp confirmed-active rows so the 26k+ soft-end-date class
                # isn't re-verified every run (24h skip in the suspects query).
                await con.executemany(
                    "UPDATE markets SET status_checked_at = now() WHERE id = $1",
                    confirmed)
        flipped += len(updates)
        await asyncio.sleep(0.1)
    return flipped


async def _sweep_polymarket(con: asyncpg.Connection, client: httpx.AsyncClient,
                            ids: list[str], *, dry_run: bool) -> int:
    flipped = 0
    sem = asyncio.Semaphore(_CONCURRENCY)
    updates: list[tuple[str, str]] = []
    confirmed: list[tuple[str]] = []

    async def one(mid: str) -> None:
        raw = mid.split(":", 1)[1]
        m = None
        async with sem:
            for base in (_GAMMA, _GAMMA_EVENTS):
                try:
                    r = await client.get(base + raw, timeout=15)
                    if r.status_code == 404:
                        continue  # market id vs event id — try the other endpoint
                    r.raise_for_status()
                    m = r.json()
                    break
                except (httpx.HTTPError, ValueError):
                    return
            await asyncio.sleep(0.1)
        if m is None:
            return
        if m.get("closed"):
            updates.append((mid, "closed"))
        else:
            confirmed.append((mid,))

    for i in range(0, len(ids), 500):
        await asyncio.gather(*(one(p) for p in ids[i:i + 500]))
    if not dry_run:
        if updates:
            await con.executemany(
                "UPDATE markets SET status = $2, status_checked_at = now() "
                "WHERE id = $1", updates)
        if confirmed:
            await con.executemany(
                "UPDATE markets SET status_checked_at = now() WHERE id = $1", confirmed)
    flipped = len(updates)
    return flipped


async def run(*, cap: int, dry_run: bool) -> None:
    con = await asyncpg.connect(_db_url(), statement_cache_size=0)
    try:
        await con.execute("SET statement_timeout = 0")
        await con.execute(
            "ALTER TABLE markets ADD COLUMN IF NOT EXISTS status_checked_at timestamptz")
        async with httpx.AsyncClient() as client:
            for venue, sweeper in (("kalshi", _sweep_kalshi),
                                   ("polymarket", _sweep_polymarket)):
                rows = await con.fetch(_SUSPECTS, venue, cap)
                ids = [r["id"] for r in rows]
                flipped = await sweeper(con, client, ids, dry_run=dry_run)
                print(f"{venue}: {len(ids)} suspects, "
                      f"{flipped} {'would flip' if dry_run else 'flipped'} to venue status")
    finally:
        await con.close()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cap", type=int, default=4000, help="max suspects per venue per run")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    asyncio.run(run(cap=args.cap, dry_run=args.dry_run))


if __name__ == "__main__":
    main()
