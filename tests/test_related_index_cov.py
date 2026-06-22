"""Coverage tests for pytheum.related.index.RelatedIndex.

Exercises the loader (file-missing, json-decode-error skip, gz round-trip,
dataset_version mtime), all lookup ref-form branches (venue-prefixed kalshi/pm,
condition_id, slug, bare, blank/non-str), relations_available, browse filters,
and the module-level get_index singleton.
"""
from __future__ import annotations

import gzip
import json
from pathlib import Path

from pytheum.related import index as related_mod
from pytheum.related.index import RelatedIndex, get_index

# --------------------------------------------------------------------------- #
# Fixture rows / file helpers
# --------------------------------------------------------------------------- #

_ROWS = [
    {
        "kalshi_ref": "kalshi:KX-BTC-100K",
        "kalshi_ticker": "KX-BTC-100K",
        "kalshi_title": "Bitcoin above 100k",
        "pm_ref": "polymarket:5001",
        "pm_gamma_id": "5001",
        "pm_condition_id": "0xABCDEF",
        "pm_slug": "btc-above-100k",
        "pm_title": "Will BTC hit 100k",
        "relation": "same_asset_date_contained",
        "asset": "BTC",
    },
    {
        "kalshi_ref": "kalshi:KX-FED",
        "kalshi_ticker": "KX-FED",
        "kalshi_title": "Fed hike 25bps",
        "pm_ref": "polymarket:5002",
        "pm_gamma_id": "5002",
        "pm_slug": "fed-decision",
        "pm_title": "Fed raises rates",
        "relation": "same_event_different_band",
        "asset": "RATES",
    },
]


def _write_gz(path: Path, rows: list[dict], *, extra_raw: list[str] | None = None) -> None:
    lines = [json.dumps(r) for r in rows]
    if extra_raw:
        lines.extend(extra_raw)
    with gzip.open(path, "wt", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")


def _loaded_index(tmp_path: Path) -> RelatedIndex:
    p = tmp_path / "related-export.jsonl.gz"
    # blank line + malformed json line exercise the skip branches.
    _write_gz(p, _ROWS, extra_raw=["", "   ", "{not json}"])
    return RelatedIndex.load(p)


# --------------------------------------------------------------------------- #
# load()
# --------------------------------------------------------------------------- #


def test_load_missing_file_degrades(tmp_path: Path) -> None:
    idx = RelatedIndex.load(tmp_path / "nope.jsonl.gz")
    assert idx.file_missing is True
    assert idx.pairs_loaded == 0


def test_load_gz_parses_and_skips_bad_lines(tmp_path: Path) -> None:
    idx = _loaded_index(tmp_path)
    assert idx.pairs_loaded == 2
    assert idx.dataset_version is not None
    assert idx.load_error is None


def test_load_plain_jsonl(tmp_path: Path) -> None:
    p = tmp_path / "related-export.jsonl"  # no .gz → plain open path
    p.write_text("\n".join(json.dumps(r) for r in _ROWS) + "\n", encoding="utf-8")
    idx = RelatedIndex.load(p)
    assert idx.pairs_loaded == 2


def test_load_catches_read_error(tmp_path: Path, monkeypatch) -> None:
    p = tmp_path / "related-export.jsonl.gz"
    _write_gz(p, _ROWS)

    def _boom(*a, **k):
        raise OSError("read failed")

    # stat succeeds (sets dataset_version) but the open/read raises → load_error.
    monkeypatch.setattr(related_mod.gzip, "open", _boom)
    idx = RelatedIndex.load(p)
    assert idx.load_error is not None
    assert idx.pairs_loaded == 0


# --------------------------------------------------------------------------- #
# lookup()
# --------------------------------------------------------------------------- #


def test_lookup_blank_and_nonstr(tmp_path: Path) -> None:
    idx = _loaded_index(tmp_path)
    assert idx.lookup("") == ([], "none")
    assert idx.lookup("   ") == ([], "none")
    assert idx.lookup(None) == ([], "none")  # type: ignore[arg-type]


def test_lookup_kalshi_prefixed(tmp_path: Path) -> None:
    idx = _loaded_index(tmp_path)
    rows, via = idx.lookup("kalshi:KX-BTC-100K")
    assert via == "kalshi_ticker" and len(rows) == 1


def test_lookup_kalshi_prefixed_miss(tmp_path: Path) -> None:
    idx = _loaded_index(tmp_path)
    assert idx.lookup("kalshi:NOPE") == ([], "none")


def test_lookup_pm_gamma(tmp_path: Path) -> None:
    idx = _loaded_index(tmp_path)
    rows, via = idx.lookup("polymarket:5001")
    assert via == "pm_gamma_id" and rows[0]["asset"] == "BTC"


def test_lookup_pm_condition_id(tmp_path: Path) -> None:
    idx = _loaded_index(tmp_path)
    rows, via = idx.lookup("polymarket:0xabcdef")
    assert via == "pm_condition_id" and len(rows) == 1


def test_lookup_pm_slug(tmp_path: Path) -> None:
    idx = _loaded_index(tmp_path)
    rows, via = idx.lookup("polymarket:btc-above-100k")
    assert via == "pm_slug"


def test_lookup_pm_miss(tmp_path: Path) -> None:
    idx = _loaded_index(tmp_path)
    assert idx.lookup("polymarket:does-not-exist") == ([], "none")


def test_lookup_bare_kalshi_ticker(tmp_path: Path) -> None:
    idx = _loaded_index(tmp_path)
    rows, via = idx.lookup("KX-FED")
    assert via == "kalshi_ticker"


def test_lookup_bare_gamma(tmp_path: Path) -> None:
    idx = _loaded_index(tmp_path)
    rows, via = idx.lookup("5002")
    assert via == "pm_gamma_id"


def test_lookup_bare_condition_id(tmp_path: Path) -> None:
    idx = _loaded_index(tmp_path)
    rows, via = idx.lookup("0xABCDEF")
    assert via == "pm_condition_id"


def test_lookup_bare_slug(tmp_path: Path) -> None:
    idx = _loaded_index(tmp_path)
    rows, via = idx.lookup("fed-decision")
    assert via == "pm_slug"


def test_lookup_bare_miss(tmp_path: Path) -> None:
    idx = _loaded_index(tmp_path)
    assert idx.lookup("totally-unknown") == ([], "none")


# --------------------------------------------------------------------------- #
# relations_available / browse
# --------------------------------------------------------------------------- #


def test_relations_available_sorted(tmp_path: Path) -> None:
    idx = _loaded_index(tmp_path)
    rels = idx.relations_available
    assert rels == sorted(rels)
    assert "same_asset_date_contained" in rels
    assert "same_event_different_band" in rels


def test_browse_no_filter(tmp_path: Path) -> None:
    idx = _loaded_index(tmp_path)
    rows, total = idx.browse()
    assert total == 2 and len(rows) == 2


def test_browse_relations_filter(tmp_path: Path) -> None:
    idx = _loaded_index(tmp_path)
    rows, total = idx.browse(relations={"same_event_different_band"})
    assert total == 1 and rows[0]["asset"] == "RATES"


def test_browse_query_substr_matches_kalshi_title(tmp_path: Path) -> None:
    idx = _loaded_index(tmp_path)
    rows, total = idx.browse(query_substr="BITCOIN")
    assert total == 1 and rows[0]["asset"] == "BTC"


def test_browse_query_substr_matches_pm_title(tmp_path: Path) -> None:
    idx = _loaded_index(tmp_path)
    rows, total = idx.browse(query_substr="raises rates")
    assert total == 1 and rows[0]["asset"] == "RATES"


def test_browse_query_substr_no_match(tmp_path: Path) -> None:
    idx = _loaded_index(tmp_path)
    rows, total = idx.browse(query_substr="zzz-nothing")
    assert total == 0 and rows == []


def test_browse_pagination(tmp_path: Path) -> None:
    idx = _loaded_index(tmp_path)
    rows, total = idx.browse(limit=1, offset=1)
    assert total == 2 and len(rows) == 1


# --------------------------------------------------------------------------- #
# get_index singleton
# --------------------------------------------------------------------------- #


def test_get_index_returns_singleton(monkeypatch) -> None:
    monkeypatch.setattr(related_mod, "_singleton", None)
    a = get_index()
    b = get_index()
    assert a is b
    assert isinstance(a, RelatedIndex)
