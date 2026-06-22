"""Coverage tests for pytheum.equivalence.index — loader + lookup-miss branches.

Fills the uncovered paths: is_fungible_method's separators-only edge case, the
gz/plain loader (skip blank + malformed lines, read-error degrade), the bare-ref
lookup miss branches, and leagues_available capping. The browse/bet_type happy
paths are covered by test_markets_matched.py already.
"""
from __future__ import annotations

import gzip
import json
from pathlib import Path

from pytheum.equivalence import index as eq_mod
from pytheum.equivalence.index import (
    EquivalenceIndex,
    get_index,
    is_fungible_method,
)

# --------------------------------------------------------------------------- #
# is_fungible_method edge cases
# --------------------------------------------------------------------------- #


def test_fungible_none_and_blank() -> None:
    assert is_fungible_method(None) is False
    assert is_fungible_method("") is False
    assert is_fungible_method("   ") is False


def test_fungible_separators_only() -> None:
    # non-empty but only separators/whitespace → no real token → not fungible.
    assert is_fungible_method(", ,") is False


def test_fungible_deterministic_true() -> None:
    assert is_fungible_method("structured_key") is True
    assert is_fungible_method("game_match") is True
    assert is_fungible_method("blocked_deterministic") is True


def test_fungible_llm_methods_false() -> None:
    assert is_fungible_method("opus_backstop") is False
    assert is_fungible_method("llm_local") is False
    assert is_fungible_method("blocked_deterministic,opus_backstop") is False
    assert is_fungible_method("blocked_deterministic,llm_judge") is False


# --------------------------------------------------------------------------- #
# loader
# --------------------------------------------------------------------------- #

_ROWS = [
    {
        "kalshi_ref": "kalshi:KX-A", "kalshi_ticker": "KX-A", "kalshi_title": "A k",
        "pm_ref": "polymarket:1", "pm_gamma_id": "1", "pm_condition_id": "0xAA",
        "pm_slug": "slug-a", "pm_title": "A p", "bet_type": "moneyline",
        "method": "structured_key", "league": "NBA", "game_date": "2026-07-01",
    },
    {
        "kalshi_ref": "kalshi:KX-B", "kalshi_ticker": "KX-B", "kalshi_title": "B k",
        "pm_ref": "polymarket:2", "pm_gamma_id": "2",
        "pm_slug": "slug-b", "pm_title": "B p", "bet_type": "total",
        "method": "opus_backstop", "league": "NFL",
    },
]


def _write_gz(p: Path) -> None:
    lines = [json.dumps(r) for r in _ROWS] + ["", "  ", "{bad json}"]
    with gzip.open(p, "wt", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")


def test_load_missing_file(tmp_path: Path) -> None:
    idx = EquivalenceIndex.load(tmp_path / "nope.jsonl.gz")
    assert idx.file_missing is True
    assert idx.pairs_loaded == 0


def test_load_gz_skips_bad_lines(tmp_path: Path) -> None:
    p = tmp_path / "equivalence-export.jsonl.gz"
    _write_gz(p)
    idx = EquivalenceIndex.load(p)
    assert idx.pairs_loaded == 2
    assert idx.dataset_version is not None
    # condition_id lowercased into its lookup dict
    rows, via = idx.lookup("polymarket:0xaa")
    assert via == "pm_condition_id" and len(rows) == 1


def test_load_plain_jsonl(tmp_path: Path) -> None:
    p = tmp_path / "equivalence-export.jsonl"
    p.write_text("\n".join(json.dumps(r) for r in _ROWS) + "\n", encoding="utf-8")
    idx = EquivalenceIndex.load(p)
    assert idx.pairs_loaded == 2


def test_load_read_error_degrades(tmp_path: Path, monkeypatch) -> None:
    p = tmp_path / "equivalence-export.jsonl.gz"
    _write_gz(p)

    def _boom(*a, **k):
        raise OSError("read failed")

    monkeypatch.setattr(eq_mod.gzip, "open", _boom)
    idx = EquivalenceIndex.load(p)
    assert idx.load_error is not None
    assert idx.pairs_loaded == 0


# --------------------------------------------------------------------------- #
# lookup miss branches
# --------------------------------------------------------------------------- #


def _idx(tmp_path: Path) -> EquivalenceIndex:
    p = tmp_path / "equivalence-export.jsonl.gz"
    _write_gz(p)
    return EquivalenceIndex.load(p)


def test_lookup_blank(tmp_path: Path) -> None:
    idx = _idx(tmp_path)
    assert idx.lookup("") == ([], "none")
    assert idx.lookup(None) == ([], "none")  # type: ignore[arg-type]


def test_lookup_kalshi_miss(tmp_path: Path) -> None:
    idx = _idx(tmp_path)
    assert idx.lookup("kalshi:NOPE") == ([], "none")


def test_lookup_pm_gamma_then_slug_fallback(tmp_path: Path) -> None:
    idx = _idx(tmp_path)
    # numeric but unknown → falls to slug lookup → miss
    assert idx.lookup("polymarket:999") == ([], "none")
    rows, via = idx.lookup("polymarket:slug-a")
    assert via == "pm_slug"


def test_lookup_bare_branches(tmp_path: Path) -> None:
    idx = _idx(tmp_path)
    assert idx.lookup("KX-A")[1] == "kalshi_ticker"
    assert idx.lookup("2")[1] == "pm_gamma_id"
    assert idx.lookup("0xAA")[1] == "pm_condition_id"
    assert idx.lookup("slug-b")[1] == "pm_slug"
    assert idx.lookup("nothing-here") == ([], "none")


# --------------------------------------------------------------------------- #
# browse filters + leagues_available
# --------------------------------------------------------------------------- #


def test_browse_league_filter_excludes_missing(tmp_path: Path) -> None:
    idx = _idx(tmp_path)
    rows, total = idx.browse(league="nba")
    assert total == 1 and rows[0]["bet_type"] == "moneyline"


def test_browse_game_date_filter_excludes_missing(tmp_path: Path) -> None:
    idx = _idx(tmp_path)
    rows, total = idx.browse(game_date="2026-07-01")
    # only row A carries game_date; row B is excluded.
    assert total == 1 and rows[0]["league"] == "NBA"


def test_browse_game_date_no_match(tmp_path: Path) -> None:
    idx = _idx(tmp_path)
    rows, total = idx.browse(game_date="2099-01-01")
    assert total == 0


def test_browse_fungible_only(tmp_path: Path) -> None:
    idx = _idx(tmp_path)
    rows, total = idx.browse(fungible_only=True)
    # row B is opus_backstop → excluded.
    assert total == 1 and rows[0]["method"] == "structured_key"


def test_leagues_available_cap(tmp_path: Path) -> None:
    idx = _idx(tmp_path)
    assert idx.leagues_available() == ["NBA", "NFL"]
    assert idx.leagues_available(max_values=1) and len(idx.leagues_available(max_values=1)) == 1


def test_get_index_singleton(monkeypatch) -> None:
    monkeypatch.setattr(eq_mod, "_singleton", None)
    a = get_index()
    assert get_index() is a
    assert isinstance(a, EquivalenceIndex)
