"""Load the FULL active market catalog (both venues) into the serving markets table.

The pairs-scoped loaders (``sync_paired_kalshi`` / ``sync_paired_polymarket`` /
``load_market_equivalence``) populate ONLY markets that sit in a verified equivalence
pair, so cross-venue SEARCH/SCREEN covers just the matched universe. This loader adds
the *rest* — every active market on each venue — so search has full breadth. The verified
twin keeps being flagged on results by the existing query-time annotator
(``pytheum.api.annotators.attach_cross_venue``, which joins ``market_equivalence``), so
"search everything; the ones with a verified equivalent are flagged" comes for free once
the breadth rows exist.

Source: the matcher's local SQLite ``markets`` DB (``--source-db``) — it already fetched
the full catalog (``lifecycle='active'``, both venues), so no venue API auth/rate-limit is
needed. We write identity + an initial book; the serving price-refresh sidecar keeps
``bestBid``/``bestAsk`` fresh once a row exists.

Non-destructive: ``ON CONFLICT (id) DO NOTHING`` — only inserts markets NOT already
present, so the pairs loaders' richer rows and the sidecar-maintained book are never
clobbered. Every inserted row carries ``payload.synced_by='full_catalog'`` and
``payload.has_verified_twin`` (computed from ``market_equivalence``) so breadth-search can
cheaply flag/filter the markets that have a verified cross-venue equivalent — the
authoritative twin (with spread) is still served by ``attach_cross_venue``.

DRY-RUN by default — pass ``--write`` to apply.

Usage:
    python -m scripts.sync_full_catalog --source-db /path/to/matcher/data/markets.db
    python -m scripts.sync_full_catalog --source-db … --venue polymarket --limit 100
    python -m scripts.sync_full_catalog --source-db … --write
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import sqlite3
from typing import Any

# asyncpg + _db_url are imported lazily inside run() (runtime-only deps), so the pure
# row mappers stay importable + unit-testable — same convention as sync_paired_kalshi.
from scripts.sync_paired_kalshi import _iso, _num

_UPSERT_NOCLOBBER = """
INSERT INTO markets (id, title, venue, status, volume_usd, liquidity_usd, url,
                     resolution_at, payload)
VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
ON CONFLICT (id) DO NOTHING
"""

_TWIN_SET_QUERY = """
SELECT kalshi_market_id AS ref FROM market_equivalence WHERE polymarket_market_id IS NOT NULL
UNION
SELECT polymarket_market_id AS ref FROM market_equivalence WHERE polymarket_market_id IS NOT NULL
"""


def _payload_json(d: dict[str, Any]) -> str:
    return json.dumps({k: v for k, v in d.items() if v is not None})


def kalshi_catalog_row(market_id: str, title: str | None, status: str | None,
                       close_date: str | None, raw_json: str | None,
                       *, twin: bool) -> tuple[Any, ...] | None:
    """Map a matcher-DB Kalshi row to a serving markets-table row (serving id
    ``kalshi:<ticker>``). Kalshi quotes YES in integer cents → serving book wants [0,1]."""
    if not market_id:
        return None
    d: dict[str, Any] = {}
    if raw_json:
        with contextlib.suppress(json.JSONDecodeError, TypeError):
            d = json.loads(raw_json)
    yes_bid, yes_ask, last = _num(d.get("yes_bid")), _num(d.get("yes_ask")), _num(d.get("last_price"))
    et = d.get("event_ticker")
    payload = _payload_json({
        "bestBid": yes_bid / 100.0 if yes_bid is not None else None,
        "bestAsk": yes_ask / 100.0 if yes_ask is not None else None,
        "lastTradePrice": last / 100.0 if last is not None else None,
        "event_ticker": et,
        "has_verified_twin": twin,
        "synced_by": "full_catalog",
    })
    return (
        f"kalshi:{market_id}", title or d.get("title"), "kalshi",
        "active" if status == "active" else "closed",
        _num(d.get("volume_fp") or d.get("volume")), _num(d.get("liquidity_dollars")),
        f"https://kalshi.com/markets/{et}" if et else None,
        _iso(close_date) or _iso(d.get("close_time")) or _iso(d.get("expiration_time")),
        payload,
    )


def polymarket_catalog_row(market_id: str, title: str | None, status: str | None,
                           close_date: str | None, raw_json: str | None,
                           *, twin: bool) -> tuple[Any, ...] | None:
    """Map a matcher-DB Polymarket row to a serving markets-table row. The matcher keys
    PM by conditionId; the serving id is ``polymarket:<gamma id>`` (raw_json['id']) — the
    same ref the equivalence export + market_equivalence use. PM gamma quotes are already
    [0,1] floats (no cents conversion)."""
    d: dict[str, Any] = {}
    if raw_json:
        with contextlib.suppress(json.JSONDecodeError, TypeError):
            d = json.loads(raw_json)
    gid = d.get("id")
    if not gid:
        return None  # no gamma id → can't form the serving ref
    slug = d.get("slug")
    payload = _payload_json({
        "bestBid": _num(d.get("bestBid")), "bestAsk": _num(d.get("bestAsk")),
        "lastTradePrice": _num(d.get("lastTradePrice")),
        "slug": slug, "condition_id": d.get("conditionId"),
        "has_verified_twin": twin, "synced_by": "full_catalog",
    })
    return (
        f"polymarket:{gid}", title or d.get("question"), "polymarket",
        "active" if status == "active" else "closed",
        _num(d.get("volumeNum") or d.get("volume")),
        _num(d.get("liquidityNum") or d.get("liquidity")),
        f"https://polymarket.com/market/{slug}" if slug else None,
        _iso(d.get("endDate")) or _iso(close_date),
        payload,
    )


_MAPPERS = {"kalshi": kalshi_catalog_row, "polymarket": polymarket_catalog_row}


def _load_catalog(source_db: str, venue: str, twin_refs: set[str],
                  limit: int | None) -> list[tuple[Any, ...]]:
    """Build serving rows for every active market of *venue* in the matcher DB."""
    con = sqlite3.connect(source_db)
    try:
        # Drive serving active/closed off `lifecycle` (the derived single source of truth),
        # NOT the raw `status` column — PM leaves status != 'active' on live markets, so a
        # status-based map mislabels active PM as closed. We filter lifecycle='active', so
        # the mapper's active-indicator arg is always 'active' here.
        q = ("SELECT market_id, title, lifecycle, close_date, raw_json FROM markets "
             "WHERE platform=? AND lifecycle='active'")
        if limit:
            q += f" LIMIT {int(limit)}"
        mapper = _MAPPERS[venue]
        rows: list[tuple[Any, ...]] = []
        for market_id, title, lifecycle, close_date, raw_json in con.execute(q, (venue,)):
            # PM's serving ref needs the gamma id (inside raw_json), so map first then
            # re-stamp has_verified_twin once we know the resulting ref.
            row = mapper(market_id, title, lifecycle, close_date, raw_json, twin=False)
            if row is None:
                continue
            if row[0] in twin_refs:
                row = mapper(market_id, title, lifecycle, close_date, raw_json, twin=True)
            rows.append(row)
        return rows
    finally:
        con.close()


async def run(*, source_db: str, venues: list[str], write: bool,
              limit: int | None, batch_size: int) -> None:
    import asyncpg

    from scripts.load_market_equivalence import _db_url
    con = await asyncpg.connect(_db_url(), statement_cache_size=0)
    try:
        await con.execute("SET statement_timeout = 0")
        twin_refs = {r["ref"] for r in await con.fetch(_TWIN_SET_QUERY)}
        print(f"verified-twin refs in market_equivalence: {len(twin_refs):,}")

        total_new = total_seen = 0
        for venue in venues:
            rows = _load_catalog(source_db, venue, twin_refs, limit)
            ids = [r[0] for r in rows]
            present = {r["id"] for r in await con.fetch(
                "SELECT id FROM markets WHERE id = ANY($1::text[])", ids)} if ids else set()
            new_rows = [r for r in rows if r[0] not in present]
            with_twin = sum(1 for r in new_rows if f'"has_verified_twin": true' in r[8])
            total_seen += len(rows)
            total_new += len(new_rows)
            print(f"[{venue}] catalog active: {len(rows):,}  already present: "
                  f"{len(present):,}  NEW to insert: {len(new_rows):,}  "
                  f"(of which verified-twin: {with_twin:,})")
            for s in new_rows[:5]:
                print(f"    would-insert: {s[0]}  status={s[3]}  title={str(s[1])[:44]!r}")
            if write and new_rows:
                for i in range(0, len(new_rows), batch_size):
                    await con.executemany(_UPSERT_NOCLOBBER, new_rows[i:i + batch_size])
                print(f"[{venue}] INSERTED {len(new_rows):,} breadth rows.")

        if not write:
            print(f"\nDRY-RUN: would insert {total_new:,} new breadth rows "
                  f"(of {total_seen:,} active catalog rows). Re-run with --write.")
        else:
            print(f"\nINSERTED {total_new:,} breadth rows. Cross-venue search now spans the "
                  f"full active catalog; attach_cross_venue flags the verified twins.")
    finally:
        await con.close()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--source-db", required=True,
                    help="matcher SQLite markets DB (the full-catalog source).")
    ap.add_argument("--venue", choices=("kalshi", "polymarket", "both"), default="both")
    ap.add_argument("--limit", type=int, default=None, help="canary: cap rows per venue.")
    ap.add_argument("--batch-size", type=int, default=2000)
    ap.add_argument("--write", action="store_true",
                    help="Actually insert. Omitted = DRY-RUN (counts + sample only).")
    args = ap.parse_args()
    venues = ["kalshi", "polymarket"] if args.venue == "both" else [args.venue]
    asyncio.run(run(source_db=args.source_db, venues=venues, write=args.write,
                    limit=args.limit, batch_size=args.batch_size))


if __name__ == "__main__":
    main()
