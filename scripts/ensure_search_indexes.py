"""Ensure the serving `markets` table has trigram indexes for fast substring search.

`/v1/markets/search` (and MCP `t_search_markets`) runs `title ILIKE '%token%'` — plus a
comma-stripped variant for numeric tokens — over the full serving `markets` table
(~459k rows). A leading-wildcard `ILIKE '%...%'` cannot use a btree index, so it falls
back to a sequential scan (the FDE audit observed 6.7s, and 13.3s cold). A GIN index with
``gin_trgm_ops`` (the ``pg_trgm`` extension) makes ``ILIKE '%...%'`` index-accelerated.

Creates, idempotently (``IF NOT EXISTS``):
  - EXTENSION ``pg_trgm``
  - GIN ``(title gin_trgm_ops)``                       — the common title path
  - GIN ``((replace(title, ',', '')) gin_trgm_ops)``   — the numeric comma-stripped path

Indexes are built ``CONCURRENTLY`` so the build never locks the live serving table — which
means each statement must run OUTSIDE a transaction (asyncpg autocommit; we never open one).
A failed CONCURRENTLY build leaves an INVALID index; drop it and re-run if that happens.

NOTE: this fixes substring search LATENCY only. Recall for paraphrase/synonyms ("controls"
vs "control", "Fed" vs "Federal Reserve") is the job of the SEMANTIC path
(`/v1/markets/relevant-to` / `t_find_markets`); a tsvector index with stemming would be a
separate, larger change.

DRY-RUN by default — prints the DDL. Pass ``--write`` to execute against the serving DB.

Usage:
  python -m scripts.ensure_search_indexes
  python -m scripts.ensure_search_indexes --write
"""
from __future__ import annotations

import argparse
import asyncio


def _statements() -> list[str]:
    """The idempotent DDL, in execution order. Each runs as its own autocommit
    statement (CONCURRENTLY forbids a surrounding transaction)."""
    return [
        "CREATE EXTENSION IF NOT EXISTS pg_trgm",
        "CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_markets_title_trgm "
        "ON markets USING gin (title gin_trgm_ops)",
        "CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_markets_title_nocomma_trgm "
        "ON markets USING gin ((replace(title, ',', '')) gin_trgm_ops)",
    ]


async def run(*, write: bool) -> None:
    stmts = _statements()
    if not write:
        print("DRY-RUN — would execute (autocommit, outside any txn):\n")
        for s in stmts:
            print(f"  {s};")
        print("\nRe-run with --write to apply against the serving DB.")
        return

    import asyncpg

    from scripts.load_market_equivalence import _db_url
    con = await asyncpg.connect(_db_url(), statement_cache_size=0)
    try:
        await con.execute("SET statement_timeout = 0")
        for s in stmts:
            print(f"executing: {s[:70]}…")
            await con.execute(s)  # autocommit — required for CREATE INDEX CONCURRENTLY
        print("\nOK: pg_trgm + trigram indexes ensured. Verify a plan with:\n"
              "  EXPLAIN ANALYZE SELECT id FROM markets WHERE title ILIKE '%bitcoin%' "
              "ORDER BY volume_usd DESC NULLS LAST LIMIT 50;\n"
              "(expect a Bitmap Index Scan on idx_markets_title_trgm, not a Seq Scan)")
    finally:
        await con.close()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--write", action="store_true",
                    help="Execute the DDL. Omitted = DRY-RUN (print only).")
    args = ap.parse_args()
    asyncio.run(run(write=args.write))


if __name__ == "__main__":
    main()
