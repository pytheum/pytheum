"""Pure-logic tests for the anon-write-hole regression guard (scripts.audit_db_grants).

The DB fetch is excluded (runtime-only asyncpg); these exercise the invariant logic with
fixture rows, including reproducing the exact 2026-06-24 hole as a regression case.
"""
from __future__ import annotations

from scripts.audit_db_grants import audit


def test_clean_state_no_violations() -> None:
    rls = [("markets", True), ("market_equivalence", True)]
    grants = [("anon", "markets", "SELECT")]
    assert audit(rls, grants) == []


def test_rls_off_is_a_violation() -> None:
    v = audit([("market_equivalence", False)], [])
    assert len(v) == 1
    assert "RLS-OFF" in v[0] and "market_equivalence" in v[0]


def test_anon_write_grant_flagged() -> None:
    v = audit([("market_equivalence", True)], [("anon", "market_equivalence", "DELETE")])
    assert any("WRITE-GRANT" in x and "DELETE" in x for x in v)


def test_every_write_priv_flagged() -> None:
    grants = [("anon", "t", p) for p in ("INSERT", "UPDATE", "DELETE", "TRUNCATE")]
    v = audit([("t", True)], grants)
    assert len(v) == 4


def test_authenticated_role_also_checked() -> None:
    v = audit([("t", True)], [("authenticated", "t", "INSERT")])
    assert any("authenticated" in x and "WRITE-GRANT" in x for x in v)


def test_select_allowlist_only_when_configured() -> None:
    rls = [("t_admin", True), ("t_internal", True)]
    grants = [("anon", "t_admin", "SELECT"), ("anon", "t_internal", "SELECT")]
    assert audit(rls, grants) == []  # no allowlist -> SELECT not checked
    v = audit(rls, grants, public_read_tables={"t_admin"})
    assert len(v) == 1 and "READ-GRANT" in v[0] and "t_internal" in v[0]


def test_reproduces_the_2026_06_24_hole() -> None:
    # The exact original exposure: RLS-off internal tables + anon full DML.
    rls = [("market_equivalence", False), ("market_price_history", False)]
    grants = [
        ("anon", "market_equivalence", "INSERT"),
        ("anon", "market_equivalence", "DELETE"),
        ("anon", "market_price_history", "UPDATE"),
    ]
    v = audit(rls, grants)
    assert sum("RLS-OFF" in x for x in v) == 2
    assert sum("WRITE-GRANT" in x for x in v) == 3


def test_violations_are_sorted_and_stable() -> None:
    v = audit([("z", False), ("a", False)], [])
    assert v == sorted(v)  # deterministic output for CI diffing
