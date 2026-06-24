"""Audit the serving Postgres for the anonymous-write hole class — a security regression guard.

2026-06-24: 7 public tables were RLS-OFF while the ``anon``/``authenticated`` roles held full
DML grants, so they were anonymously read/write/TRUNCATE-able via Supabase PostgREST (the matcher
gold set, price history, etc.). The acute hole was patched (RLS enabled + grants revoked), but
Supabase RE-GRANTS ``anon``/``authenticated`` full DML on every NEWLY-created table by default —
so the next migration silently re-opens it. This is the standing guard: it asserts the two
invariants that keep the hole shut and exits nonzero (listing the offenders) the moment either
drifts, so it can run in CI or a weekly cron regardless of who adds a table.

Invariants:
  1. Every table in ``public`` has Row-Level Security ENABLED.
  2. Neither ``anon`` nor ``authenticated`` holds any WRITE grant (INSERT/UPDATE/DELETE/TRUNCATE/
     REFERENCES/TRIGGER) on any table. Optionally, they may SELECT only allowlisted tables.

The pure ``audit()`` takes already-fetched rows so it is importable + unit-testable with no DB
(mirrors why ``sync_paired_kalshi`` splits its row mapper out). ``run()`` does the live fetch.

Usage:
    python -m scripts.audit_db_grants                          # exit 1 on any violation
    python -m scripts.audit_db_grants --json                   # machine-readable (CI)
    python -m scripts.audit_db_grants --public-read-table markets --public-read-table ...
"""
from __future__ import annotations

import argparse
import asyncio
import json

# asyncpg + _db_url are imported lazily inside run() — runtime-only deps kept out of module
# import so the pure audit() stays importable + unit-testable without a database.

_WRITE_PRIVS = frozenset({"INSERT", "UPDATE", "DELETE", "TRUNCATE", "REFERENCES", "TRIGGER"})

_RLS_QUERY = """
SELECT relname, relrowsecurity
FROM pg_class
WHERE relnamespace = 'public'::regnamespace AND relkind = 'r'
ORDER BY relname
"""

_GRANTS_QUERY = """
SELECT grantee, table_name, privilege_type
FROM information_schema.role_table_grants
WHERE grantee IN ('anon', 'authenticated') AND table_schema = 'public'
ORDER BY table_name, grantee, privilege_type
"""


def audit(
    rls_rows: list[tuple[str, bool]],
    grant_rows: list[tuple[str, str, str]],
    *,
    public_read_tables: set[str] | None = None,
) -> list[str]:
    """Return a sorted list of violation strings (empty == clean).

    rls_rows:   (table_name, rls_enabled) for every table in ``public``.
    grant_rows: (grantee, table_name, privilege_type) for anon/authenticated grants.
    public_read_tables: if given, anon/authenticated SELECT is allowed ONLY on these tables;
        if None, SELECT grants are not checked (RLS + write-grant invariants only).
    """
    violations: list[str] = []
    for name, rls_on in rls_rows:
        if not rls_on:
            violations.append(f"RLS-OFF: public.{name} (anon-exposed via PostgREST)")
    for grantee, table, priv in grant_rows:
        p = (priv or "").upper()
        if p in _WRITE_PRIVS:
            violations.append(f"WRITE-GRANT: {grantee} has {p} on public.{table} (revoke it)")
        elif p == "SELECT" and public_read_tables is not None and table not in public_read_tables:
            violations.append(f"READ-GRANT: {grantee} can SELECT public.{table} (not allowlisted)")
    return sorted(violations)


async def run(*, as_json: bool, public_read_tables: set[str] | None) -> int:
    import asyncpg

    from scripts.load_market_equivalence import _db_url
    con = await asyncpg.connect(_db_url(), statement_cache_size=0)
    try:
        rls = [(r["relname"], r["relrowsecurity"]) for r in await con.fetch(_RLS_QUERY)]
        grants = [(r["grantee"], r["table_name"], r["privilege_type"])
                  for r in await con.fetch(_GRANTS_QUERY)]
    finally:
        await con.close()

    violations = audit(rls, grants, public_read_tables=public_read_tables)
    if as_json:
        print(json.dumps({"ok": not violations, "public_tables": len(rls),
                          "anon_grants": len(grants), "violations": violations}, indent=2))
    else:
        print(f"audited {len(rls)} public tables, {len(grants)} anon/authenticated grants")
        if violations:
            print(f"\nFAIL: {len(violations)} violation(s) — the anon-write hole is open or drifting:")
            for v in violations:
                print(f"  - {v}")
        else:
            print("OK: all public tables RLS-ON; no anon/authenticated write grants.")
    return 1 if violations else 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    ap.add_argument("--public-read-table", action="append", default=None,
                    dest="public_read_tables",
                    help="table anon/authenticated may SELECT (repeatable); "
                         "omit to skip the SELECT-tightness check")
    args = ap.parse_args()
    prt = set(args.public_read_tables) if args.public_read_tables else None
    return asyncio.run(run(as_json=args.json, public_read_tables=prt))


if __name__ == "__main__":
    raise SystemExit(main())
