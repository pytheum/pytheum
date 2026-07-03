"""Load the cross-venue matcher's gold pairs into Supabase `market_equivalence`.

The matcher (pytheum-cross-venue-matcher, cofounder's repo) ships its full
pre-decided pair set as `data/dataset-all.jsonl.gz` — 132,946 gold pairs as of
2026-06-11, strictly 1:1, each carrying the `method` that produced it
(spread_match / game_title_match / human_adjudicated / ...) and confidence on
the judged slice. We CONSUME those pairs read-only; no matching happens here.

Key mapping: `kalshi_ticker` is a public natural key (-> kalshi:<ticker>).
`polymarket_id` is HIS DB-local id ("not usable against the public APIs", per
his build_dataset.py) — the public poly key is `polymarket_slug`, which we
resolve against our markets.url suffix at load time. Unresolved rows are kept
with a NULL polymarket_market_id so re-running the loader (wider coverage on
our side, or conditionId in a future export) lifts resolution without code
changes. 2026-06-11 baseline: 132,946 loaded / ~30k slug-resolved / 4,919
both-legs-active.

Refresh semantics: each load UPSERTs — new (kalshi, slug) pairs are inserted, and
EXISTING rows are re-resolved (polymarket_market_id, metadata, loaded_at) against
the CURRENT markets table. This is load-bearing: Polymarket recycles gamma ids, so
a row's slug->markets.id resolution goes stale over time; the prior DO-NOTHING made
re-runs no-ops, freezing the base at its first load (the 2026-06-11 → 1.7%-resolve
drift that darkened the matched arb radar). Upsert self-heals every row per load.

Usage:
    python -m scripts.load_market_equivalence \
        --in /path/to/dataset-all.jsonl.gz --source-commit <sha>
"""
from __future__ import annotations

import argparse
import asyncio
import gzip
import json
import os
import sys
from pathlib import Path
from typing import Any

import asyncpg

_DDL = """
DROP TABLE IF EXISTS market_equivalence;
CREATE TABLE market_equivalence (
    kalshi_market_id     text NOT NULL,
    polymarket_slug      text,
    polymarket_market_id text,
    bet_type             text,
    method               text,
    slice                text,
    confidence           real,
    domain               text,
    source_commit        text,
    loaded_at            timestamptz NOT NULL DEFAULT now(),
    UNIQUE (kalshi_market_id, polymarket_slug)
);
CREATE INDEX market_equivalence_kalshi_idx
    ON market_equivalence (kalshi_market_id);
CREATE INDEX market_equivalence_poly_idx
    ON market_equivalence (polymarket_market_id)
    WHERE polymarket_market_id IS NOT NULL;
"""


def record_to_row(rec: dict, source_commit: str | None) -> tuple | None:
    """Map one matcher dataset record to a market_equivalence row (unresolved)."""
    ticker = rec.get("kalshi_ticker")
    slug = rec.get("polymarket_slug")
    if not ticker or not slug:
        return None
    return (
        f"kalshi:{ticker}",
        slug,
        rec.get("bet_type"),
        rec.get("method"),
        rec.get("slice"),
        rec.get("confidence"),
        rec.get("domain"),
        source_commit,
    )


def resolve_slug(slug: str, url_index: dict[str, str]) -> str | None:
    """Resolve a Polymarket market slug to our market id via the url suffix."""
    return url_index.get(slug)


def _url_slug(url: str) -> str:
    return url.rstrip("/").rsplit("/", 1)[-1]


def _db_url() -> str:
    url = os.environ.get("SUPABASE_DB_URL")
    if url:
        return url
    # Lightweight .env fallback for laptop-side runs.
    env_path = Path(__file__).resolve().parent.parent / ".env"
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            if line.startswith("SUPABASE_DB_URL="):
                return line.split("=", 1)[1].strip().strip('"')
    print("SUPABASE_DB_URL not set and no .env found", file=sys.stderr)
    raise SystemExit(2)


async def load(path: Path, source_commit: str | None) -> None:
    raw: list[tuple] = []
    opener: Any = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rt") as f:
        for line in f:
            row = record_to_row(json.loads(line), source_commit)
            if row is not None:
                raw.append(row)
    print(f"parsed {len(raw)} pairs from {path}")

    con = await asyncpg.connect(_db_url(), statement_cache_size=0)
    try:
        await con.execute("SET statement_timeout = 0")
        purls = await con.fetch(
            "SELECT id, url FROM markets WHERE venue='polymarket' AND url IS NOT NULL"
        )
        url_index = {_url_slug(r["url"]): r["id"] for r in purls}
        # Gamma-resolved cache (scripts/resolve_polymarket_slugs) — survives this
        # loader's TRUNCATE, so re-loads re-apply prior API resolutions for free.
        try:
            cached = await con.fetch("SELECT slug, market_id FROM polymarket_slug_map")
        except asyncpg.UndefinedTableError:
            cached = []
        for r in cached:
            url_index.setdefault(r["slug"], r["market_id"])
        rows = []
        resolved = 0
        for kid, slug, *rest in raw:
            pid = resolve_slug(slug, url_index)
            if pid is not None:
                resolved += 1
            rows.append((kid, slug, pid, *rest))
        print(f"resolved {resolved}/{len(rows)} poly slugs against markets.url")

        await con.execute(_DDL)
        async with con.transaction():
            await con.executemany(
                "INSERT INTO market_equivalence "
                "(kalshi_market_id, polymarket_slug, polymarket_market_id, bet_type, "
                " method, slice, confidence, domain, source_commit) "
                "VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9) "
                # Upsert-REFRESH, not DO NOTHING: an existing row's
                # polymarket_market_id is a slug->markets.id resolution frozen at
                # its last load. Polymarket recycles gamma ids, so DO NOTHING let
                # rows go stale — the 2026-06-11 base drifted to 1.7% active-resolve
                # over 22 days because re-runs never refreshed it (matched arb radar
                # went dark). Re-resolve every row against current markets on each
                # load so the table self-heals; loaded_at tracks real freshness.
                "ON CONFLICT (kalshi_market_id, polymarket_slug) DO UPDATE SET "
                "  polymarket_market_id = EXCLUDED.polymarket_market_id, "
                "  bet_type = EXCLUDED.bet_type, method = EXCLUDED.method, "
                "  slice = EXCLUDED.slice, confidence = EXCLUDED.confidence, "
                "  domain = EXCLUDED.domain, source_commit = EXCLUDED.source_commit, "
                "  loaded_at = now()",
                rows,
            )
        n = await con.fetchval("SELECT count(*) FROM market_equivalence")
        live = await con.fetchval(
            "SELECT count(*) FROM market_equivalence e "
            "JOIN markets ka ON ka.id = e.kalshi_market_id AND ka.status='active' "
            "JOIN markets pa ON pa.id = e.polymarket_market_id AND pa.status='active'"
        )
        print(f"market_equivalence: {n} pairs loaded, {live} with both legs active")
    finally:
        await con.close()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--in", dest="path", required=True, type=Path)
    ap.add_argument("--source-commit", default=None)
    args = ap.parse_args()
    asyncio.run(load(args.path, args.source_commit))


if __name__ == "__main__":
    main()
