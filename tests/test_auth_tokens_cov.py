"""Coverage tests for pytheum.auth.tokens — TokenStore + helpers."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from pytheum.auth.tokens import (
    Token,
    TokenStore,
    _sha256,
    generate_token,
)

# ---------------------------------------------------------------------------
# generate_token / _sha256
# ---------------------------------------------------------------------------


def test_generate_token_shape() -> None:
    tok = generate_token()
    parts = tok.split("_")
    assert parts[0] == "pyth"
    assert len(parts) == 3
    assert len(parts[1]) == 8  # 4 hex bytes
    assert len(parts[2]) == 32  # 16 hex bytes


def test_generate_token_unique() -> None:
    assert generate_token() != generate_token()


def test_sha256_stable_and_hex() -> None:
    a = _sha256("hello")
    b = _sha256("hello")
    assert a == b
    assert len(a) == 64
    assert _sha256("hello") != _sha256("world")


# ---------------------------------------------------------------------------
# issue / verify
# ---------------------------------------------------------------------------


def test_issue_returns_plaintext_and_persists(tmp_path: Path) -> None:
    store = TokenStore(tmp_path / "tokens.jsonl")
    plain = store.issue(owner_email="a@b.com", tier="demo")
    assert plain.startswith("pyth_")
    # Persisted to disk
    lines = (tmp_path / "tokens.jsonl").read_text().splitlines()
    assert len(lines) == 1
    row = json.loads(lines[0])
    assert row["owner_email"] == "a@b.com"
    assert row["tier"] == "demo"
    assert row["revoked"] is False
    # Plaintext is NOT written to disk
    assert plain not in lines[0]
    assert row["token_sha256"] == _sha256(plain)


def test_issue_creates_parent_dir(tmp_path: Path) -> None:
    nested = tmp_path / "deep" / "nested" / "tokens.jsonl"
    store = TokenStore(nested)
    store.issue(owner_email="x@y.com", tier="issued")
    assert nested.exists()


def test_verify_accepts_valid_token(tmp_path: Path) -> None:
    store = TokenStore(tmp_path / "t.jsonl")
    plain = store.issue(owner_email="who@x.com", tier="issued")
    tok = store.verify(plain)
    assert tok is not None
    assert tok.owner_email == "who@x.com"
    assert tok.tier == "issued"
    assert tok.revoked is False


def test_verify_rejects_empty_and_bad_prefix(tmp_path: Path) -> None:
    store = TokenStore(tmp_path / "t.jsonl")
    assert store.verify("") is None
    assert store.verify("bearer_abc") is None
    assert store.verify("pyth") is None  # startswith pyth but not pyth_


def test_verify_rejects_unknown_token(tmp_path: Path) -> None:
    store = TokenStore(tmp_path / "t.jsonl")
    store.issue(owner_email="a@b.com", tier="demo")
    # Well-formed but never issued
    assert store.verify(generate_token()) is None


def test_verify_rejects_revoked_token(tmp_path: Path) -> None:
    store = TokenStore(tmp_path / "t.jsonl")
    plain = store.issue(owner_email="a@b.com", tier="demo")
    prefix = plain.split("_")[1]
    assert store.revoke(prefix) is True
    assert store.verify(plain) is None


# ---------------------------------------------------------------------------
# revoke
# ---------------------------------------------------------------------------


def test_revoke_returns_false_for_unknown_prefix(tmp_path: Path) -> None:
    store = TokenStore(tmp_path / "t.jsonl")
    store.issue(owner_email="a@b.com", tier="demo")
    assert store.revoke("deadbeef") is False


def test_revoke_idempotent_second_call_false(tmp_path: Path) -> None:
    store = TokenStore(tmp_path / "t.jsonl")
    plain = store.issue(owner_email="a@b.com", tier="demo")
    prefix = plain.split("_")[1]
    assert store.revoke(prefix) is True
    # Already revoked → not found among non-revoked
    assert store.revoke(prefix) is False


def test_revoke_appends_audit_row(tmp_path: Path) -> None:
    p = tmp_path / "t.jsonl"
    store = TokenStore(p)
    plain = store.issue(owner_email="a@b.com", tier="demo")
    prefix = plain.split("_")[1]
    store.revoke(prefix)
    lines = p.read_text().splitlines()
    # issue + revoke append → 2 rows
    assert len(lines) == 2
    assert json.loads(lines[1])["revoked"] is True


# ---------------------------------------------------------------------------
# list
# ---------------------------------------------------------------------------


def test_list_returns_all_tokens(tmp_path: Path) -> None:
    store = TokenStore(tmp_path / "t.jsonl")
    store.issue(owner_email="a@b.com", tier="demo")
    store.issue(owner_email="c@d.com", tier="issued")
    toks = store.list()
    assert len(toks) == 2
    assert {t.owner_email for t in toks} == {"a@b.com", "c@d.com"}
    assert all(isinstance(t, Token) for t in toks)


# ---------------------------------------------------------------------------
# _load — persistence round-trip + malformed handling
# ---------------------------------------------------------------------------


def test_load_roundtrip_from_disk(tmp_path: Path) -> None:
    p = tmp_path / "t.jsonl"
    store = TokenStore(p)
    plain = store.issue(owner_email="round@trip.com", tier="issued")
    # New store reading the same file
    store2 = TokenStore(p)
    tok = store2.verify(plain)
    assert tok is not None
    assert tok.owner_email == "round@trip.com"


def test_load_missing_file_is_empty(tmp_path: Path) -> None:
    store = TokenStore(tmp_path / "does_not_exist.jsonl")
    assert store.list() == []


def test_load_skips_blank_and_malformed_lines(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    p = tmp_path / "t.jsonl"
    good = {
        "token_sha256": _sha256("pyth_aaaaaaaa_" + "b" * 32),
        "prefix": "aaaaaaaa",
        "tier": "demo",
        "owner_email": "g@h.com",
        "created_at": 1.0,
        "revoked": False,
    }
    p.write_text(
        "\n"  # blank line
        + "   \n"  # whitespace-only line
        + "not json at all\n"  # JSONDecodeError
        + json.dumps({"prefix": "x"})  # KeyError (missing fields)
        + "\n"
        + json.dumps(good)
        + "\n"
    )
    store = TokenStore(p)
    # Only the one good row survives
    assert len(store.list()) == 1
    assert store.list()[0].owner_email == "g@h.com"


def test_load_revoked_flag_preserved(tmp_path: Path) -> None:
    p = tmp_path / "t.jsonl"
    row = {
        "token_sha256": "abc",
        "prefix": "pp",
        "tier": "demo",
        "owner_email": "r@x.com",
        "created_at": 5.0,
        "revoked": True,
    }
    p.write_text(json.dumps(row) + "\n")
    store = TokenStore(p)
    toks = store.list()
    assert len(toks) == 1
    assert toks[0].revoked is True
