"""Resolve market_equivalence Polymarket slugs to market ids via the Gamma API.

Self-serve alternative to waiting for conditionId in the matcher export: Gamma
answers `GET /markets?slug=<slug>` (add `closed=true` to include settled
markets) with the market's numeric id + conditionId. Hits are cached in
`polymarket_slug_map`, which survives the loader's TRUNCATE of
market_equivalence — re-running the loader re-applies the map.

Default scope: unresolved pairs whose KALSHI leg exists in our markets table
(the binding side — ~3k slugs), so a run is minutes, not hours. `--all`
sweeps the full corpus.

Usage:
    python -m scripts.resolve_polymarket_slugs [--all] [--limit N]
"""
from __future__ import annotations

import argparse
import asyncio

import asyncpg
import httpx

from scripts.load_market_equivalence import _db_url

_GAMMA = "https://gamma-api.polymarket.com/markets"
_CONCURRENCY = 4
_DDL = """
CREATE TABLE IF NOT EXISTS polymarket_slug_map (
    slug         text PRIMARY KEY,
    market_id    text NOT NULL,
    condition_id text,
    resolved_at  timestamptz NOT NULL DEFAULT now()
);
"""

_SCOPED_QUERY = """
SELECT DISTINCT e.polymarket_slug
FROM market_equivalence e
JOIN markets ka ON ka.id = e.kalshi_market_id
LEFT JOIN polymarket_slug_map m ON m.slug = e.polymarket_slug
WHERE e.polymarket_market_id IS NULL AND m.slug IS NULL
"""

_ALL_QUERY = """
SELECT DISTINCT e.polymarket_slug
FROM market_equivalence e
LEFT JOIN polymarket_slug_map m ON m.slug = e.polymarket_slug
WHERE e.polymarket_market_id IS NULL AND m.slug IS NULL
"""

_APPLY = """
UPDATE market_equivalence e
SET polymarket_market_id = m.market_id
FROM polymarket_slug_map m
WHERE m.slug = e.polymarket_slug AND e.polymarket_market_id IS NULL
"""


async def _lookup(client: httpx.AsyncClient, slug: str) -> tuple[str, str] | None:
    """Return (market_id, condition_id) for a slug, or None. Tries the bare
    query first, then closed=true (Gamma omits settled markets by default)."""
    for params in ({"slug": slug}, {"slug": slug, "closed": "true"}):
        try:
            r = await client.get(_GAMMA, params=params, timeout=15)
            r.raise_for_status()
            hits = r.json()
        except (httpx.HTTPError, ValueError):
            return None
        if isinstance(hits, list) and hits:
            m = hits[0]
            mid = m.get("id")
            if mid is not None:
                return f"polymarket:{mid}", m.get("conditionId")
    return None


async def run(*, scope_all: bool, limit: int | None) -> None:
    con = await asyncpg.connect(_db_url(), statement_cache_size=0)
    try:
        await con.execute("SET statement_timeout = 0")
        await con.execute(_DDL)
        q = _ALL_QUERY if scope_all else _SCOPED_QUERY
        if limit:
            q += f" LIMIT {int(limit)}"
        slugs = [r["polymarket_slug"] for r in await con.fetch(q)]
        print(f"resolving {len(slugs)} slugs via Gamma "
              f"({'full corpus' if scope_all else 'kalshi-known scope'})")

        sem = asyncio.Semaphore(_CONCURRENCY)
        hits: list[tuple[str, str, str | None]] = []
        misses = 0

        async with httpx.AsyncClient() as client:
            async def one(slug: str) -> None:
                nonlocal misses
                async with sem:
                    res = await _lookup(client, slug)
                    await asyncio.sleep(0.1)  # stay polite on Gamma
                if res is None:
                    misses += 1
                else:
                    hits.append((slug, res[0], res[1]))

            for i in range(0, len(slugs), 500):
                await asyncio.gather(*(one(s) for s in slugs[i:i + 500]))
                print(f"  {min(i + 500, len(slugs))}/{len(slugs)} "
                      f"(hits={len(hits)} misses={misses})")

        if hits:
            await con.executemany(
                "INSERT INTO polymarket_slug_map (slug, market_id, condition_id) "
                "VALUES ($1, $2, $3) ON CONFLICT (slug) DO NOTHING",
                hits,
            )
        applied = await con.execute(_APPLY)
        n_live = await con.fetchval(
            "SELECT count(*) FROM market_equivalence e "
            "JOIN markets ka ON ka.id = e.kalshi_market_id AND ka.status='active' "
            "JOIN markets pa ON pa.id = e.polymarket_market_id AND pa.status='active'"
        )
        print(f"hits={len(hits)} misses={misses} | {applied} | "
              f"both-legs-active pairs now: {n_live}")
    finally:
        await con.close()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--all", action="store_true", help="sweep the full corpus")
    ap.add_argument("--limit", type=int, default=None)
    args = ap.parse_args()
    asyncio.run(run(scope_all=args.all, limit=args.limit))


if __name__ == "__main__":
    main()
