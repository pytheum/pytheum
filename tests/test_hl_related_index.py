"""Tests for pytheum.related.hl_index.HLRelatedIndex + the MCP tool attachment.

Covers: gz-fixture load, resolution by every leg-identifier key form (kalshi
ticker, pm gamma_id, pm condition_id, pm slug, hyperliquid native_id), the
missing-file degradation, the get_index singleton, and the related_markets
tool's opt-in include_hyperliquid attachment (rows + note; missing-file flag;
default-response zero regression). No network: tools._get_market is patched.
"""
from __future__ import annotations

import gzip
import json
from pathlib import Path
from typing import Any

import pytest

import pytheum.mcp.tools as tools
from pytheum.related import hl_index as hl_mod
from pytheum.related.hl_index import HLRelatedIndex, get_index

# --------------------------------------------------------------------------- #
# Fixture rows / file helpers
# --------------------------------------------------------------------------- #

_ROWS = [
    {   # World Cup outright — kalshi + hyperliquid legs
        "legs": [
            {"venue": "kalshi", "ref": "kalshi:KXWC-26-BRA",
             "native_id": "KXWC-26-BRA", "title": "Will Brazil win the 2026 World Cup?"},
            {"venue": "hyperliquid", "ref": "hyperliquid:WC-BRA",
             "native_id": "WC-BRA", "title": "Brazil to win the World Cup",
             "implied_yes": 0.24, "as_of": "2026-07-01T04:00:00Z"},
        ],
        "kalshi_ref": "kalshi:KXWC-26-BRA",
        "kalshi_native_id": "KXWC-26-BRA",
        "kalshi_title": "Will Brazil win the 2026 World Cup?",
        "hyperliquid_ref": "hyperliquid:WC-BRA",
        "hyperliquid_native_id": "WC-BRA",
        "hyperliquid_title": "Brazil to win the World Cup",
        "tier": "related",
        "relation": "wc_outright_winner",
        "settlement": "equivalent",
        "country": "BRA",
    },
    {   # World Cup outright — polymarket + hyperliquid legs
        "legs": [
            {"venue": "polymarket", "ref": "polymarket:91001",
             "native_id": "0xDEADBEEF", "gamma_id": "91001",
             "slug": "will-argentina-win-the-world-cup",
             "title": "Will Argentina win the 2026 World Cup?"},
            {"venue": "hyperliquid", "ref": "hyperliquid:WC-ARG",
             "native_id": "WC-ARG", "title": "Argentina to win the World Cup",
             "implied_yes": 0.31, "as_of": "2026-07-01T04:00:00Z"},
        ],
        "polymarket_ref": "polymarket:91001",
        "polymarket_native_id": "0xDEADBEEF",
        "polymarket_title": "Will Argentina win the 2026 World Cup?",
        "hyperliquid_ref": "hyperliquid:WC-ARG",
        "hyperliquid_native_id": "WC-ARG",
        "hyperliquid_title": "Argentina to win the World Cup",
        "tier": "related",
        "relation": "wc_outright_winner",
        "settlement": "equivalent",
        "country": "ARG",
    },
    {   # Crypto threshold — kalshi + hyperliquid legs, divergent bands
        "legs": [
            {"venue": "kalshi", "ref": "kalshi:KXBTC-25DEC-100K",
             "native_id": "KXBTC-25DEC-100K", "title": "Bitcoin above $100k on Dec 31?"},
            {"venue": "hyperliquid", "ref": "hyperliquid:BTC-100K-DEC",
             "native_id": "BTC-100K-DEC", "title": "BTC >= $100k Dec 31",
             "implied_yes": 0.61, "as_of": "2026-07-01T04:00:00Z"},
        ],
        "kalshi_ref": "kalshi:KXBTC-25DEC-100K",
        "kalshi_native_id": "KXBTC-25DEC-100K",
        "kalshi_title": "Bitcoin above $100k on Dec 31?",
        "hyperliquid_ref": "hyperliquid:BTC-100K-DEC",
        "hyperliquid_native_id": "BTC-100K-DEC",
        "hyperliquid_title": "BTC >= $100k Dec 31",
        "tier": "related",
        "relation": "crypto_threshold_in_band_divergent",
        "asset": "BTC",
        "settle_delta_hours": 4,
        "basis_note": "Same threshold; HL settles on a different index print.",
    },
]


def _write_gz(path: Path, rows: list[dict[str, Any]],
              *, extra_raw: list[str] | None = None) -> None:
    lines = [json.dumps(r) for r in rows]
    if extra_raw:
        lines.extend(extra_raw)
    with gzip.open(path, "wt", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")


def _loaded_index(tmp_path: Path) -> HLRelatedIndex:
    p = tmp_path / "hl-related-export.jsonl.gz"
    # blank line + malformed json line exercise the skip branches.
    _write_gz(p, _ROWS, extra_raw=["", "   ", "{not json}"])
    return HLRelatedIndex.load(p)


# --------------------------------------------------------------------------- #
# load()
# --------------------------------------------------------------------------- #


def test_load_missing_file_degrades(tmp_path: Path) -> None:
    idx = HLRelatedIndex.load(tmp_path / "nope.jsonl.gz")
    assert idx.file_missing is True
    assert idx.pairs_loaded == 0
    assert idx.dataset_version is None
    assert idx.rows_for_ref("kalshi:KXWC-26-BRA") == []


def test_load_gz_parses_and_skips_bad_lines(tmp_path: Path) -> None:
    idx = _loaded_index(tmp_path)
    assert idx.pairs_loaded == 3
    assert idx.dataset_version is not None  # file-mtime ISO fallback
    assert idx.load_error is None
    assert idx.file_missing is False


def test_load_plain_jsonl(tmp_path: Path) -> None:
    p = tmp_path / "hl-related-export.jsonl"  # no .gz → plain open path
    p.write_text("\n".join(json.dumps(r) for r in _ROWS) + "\n", encoding="utf-8")
    idx = HLRelatedIndex.load(p)
    assert idx.pairs_loaded == 3


def test_load_meta_row_sets_dataset_version(tmp_path: Path) -> None:
    p = tmp_path / "hl-related-export.jsonl.gz"
    meta = {"_meta": {"dataset_version": "2026-07-01T04:00:00Z"}}
    _write_gz(p, [meta, *_ROWS])
    idx = HLRelatedIndex.load(p)
    assert idx.dataset_version == "2026-07-01T04:00:00Z"
    assert idx.pairs_loaded == 3  # the meta row is not a pair


def test_load_catches_read_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    p = tmp_path / "hl-related-export.jsonl.gz"
    _write_gz(p, _ROWS)

    def _boom(*a: Any, **k: Any) -> Any:
        raise OSError("read failed")

    # stat succeeds (sets dataset_version) but the open/read raises → load_error.
    monkeypatch.setattr(hl_mod.gzip, "open", _boom)
    idx = HLRelatedIndex.load(p)
    assert idx.load_error is not None
    assert idx.pairs_loaded == 0


# --------------------------------------------------------------------------- #
# rows_for_ref() — every leg-identifier key form
# --------------------------------------------------------------------------- #


def test_rows_for_ref_blank_and_nonstr(tmp_path: Path) -> None:
    idx = _loaded_index(tmp_path)
    assert idx.rows_for_ref("") == []
    assert idx.rows_for_ref("   ") == []
    assert idx.rows_for_ref(None) == []  # type: ignore[arg-type]


def test_rows_for_ref_kalshi_ticker_prefixed(tmp_path: Path) -> None:
    idx = _loaded_index(tmp_path)
    rows = idx.rows_for_ref("kalshi:KXWC-26-BRA")
    assert len(rows) == 1 and rows[0]["relation"] == "wc_outright_winner"
    assert rows[0] == _ROWS[0]  # rows come back verbatim


def test_rows_for_ref_kalshi_ticker_bare(tmp_path: Path) -> None:
    idx = _loaded_index(tmp_path)
    rows = idx.rows_for_ref("KXBTC-25DEC-100K")
    assert len(rows) == 1 and rows[0]["asset"] == "BTC"


def test_rows_for_ref_pm_gamma_id(tmp_path: Path) -> None:
    idx = _loaded_index(tmp_path)
    rows = idx.rows_for_ref("polymarket:91001")
    assert len(rows) == 1 and rows[0]["country"] == "ARG"


def test_rows_for_ref_pm_condition_id_case_insensitive(tmp_path: Path) -> None:
    idx = _loaded_index(tmp_path)
    rows = idx.rows_for_ref("polymarket:0xdeadbeef")  # stored as 0xDEADBEEF
    assert len(rows) == 1 and rows[0]["country"] == "ARG"
    assert idx.rows_for_ref("0xDEADBEEF") == rows  # bare form too


def test_rows_for_ref_pm_slug(tmp_path: Path) -> None:
    idx = _loaded_index(tmp_path)
    rows = idx.rows_for_ref("polymarket:will-argentina-win-the-world-cup")
    assert len(rows) == 1 and rows[0]["country"] == "ARG"


def test_rows_for_ref_hl_native_id(tmp_path: Path) -> None:
    idx = _loaded_index(tmp_path)
    rows = idx.rows_for_ref("hyperliquid:WC-BRA")
    assert len(rows) == 1 and rows[0]["country"] == "BRA"
    assert idx.rows_for_ref("BTC-100K-DEC")[0]["asset"] == "BTC"  # bare form


def test_rows_for_ref_hl_leg_carries_snapshot_fields(tmp_path: Path) -> None:
    idx = _loaded_index(tmp_path)
    (row,) = idx.rows_for_ref("kalshi:KXBTC-25DEC-100K")
    hl_leg = next(leg for leg in row["legs"] if leg["venue"] == "hyperliquid")
    assert hl_leg["implied_yes"] == 0.61
    assert hl_leg["as_of"] == "2026-07-01T04:00:00Z"


def test_rows_for_ref_miss(tmp_path: Path) -> None:
    idx = _loaded_index(tmp_path)
    assert idx.rows_for_ref("kalshi:NOPE") == []
    assert idx.rows_for_ref("polymarket:does-not-exist") == []
    assert idx.rows_for_ref("hyperliquid:NOPE") == []
    assert idx.rows_for_ref("totally-unknown") == []


def test_rows_for_ref_flattened_fields_fallback(tmp_path: Path) -> None:
    """A row without a legs list is still indexed from its flattened fields."""
    flat = {
        "kalshi_native_id": "KXFLAT-1",
        "polymarket_native_id": "0xFLAT",
        "hyperliquid_native_id": "FLAT-HL",
        "relation": "wc_outright_winner",
    }
    p = tmp_path / "hl-related-export.jsonl.gz"
    _write_gz(p, [flat])
    idx = HLRelatedIndex.load(p)
    assert idx.rows_for_ref("kalshi:KXFLAT-1") == [flat]
    assert idx.rows_for_ref("polymarket:0xflat") == [flat]
    assert idx.rows_for_ref("hyperliquid:FLAT-HL") == [flat]


# --------------------------------------------------------------------------- #
# get_index singleton
# --------------------------------------------------------------------------- #


def test_get_index_returns_singleton(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(hl_mod, "_singleton", None)
    a = get_index()
    b = get_index()
    assert a is b
    assert isinstance(a, HLRelatedIndex)


# --------------------------------------------------------------------------- #
# related_markets tool — include_hyperliquid attachment (no network)
# --------------------------------------------------------------------------- #

_CANNED_RELATED = {
    "market": {"id": "kalshi:KXWC-26-BRA", "venue": "kalshi"},
    "related": [{"id": "polymarket:99001", "relation": "same_event_different_band"}],
    "count": 1,
    "meta": {"pairs_loaded": 1097},
}


def _patch_http(monkeypatch: pytest.MonkeyPatch, captured: dict[str, Any]) -> None:
    async def _fake_get_market(path: str, params: dict[str, Any], base_url: str,
                               **kw: Any) -> dict[str, Any]:
        captured["path"] = path
        return json.loads(json.dumps(_CANNED_RELATED))  # fresh copy per call

    monkeypatch.setattr(tools, "_get_market", _fake_get_market)


def _patch_hl_singleton(monkeypatch: pytest.MonkeyPatch, idx: HLRelatedIndex) -> None:
    monkeypatch.setattr(hl_mod, "_singleton", idx)


async def test_tool_include_hyperliquid_attaches_rows_and_note(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    cap: dict[str, Any] = {}
    _patch_http(monkeypatch, cap)
    _patch_hl_singleton(monkeypatch, _loaded_index(tmp_path))
    resp = await tools.related_markets(
        "kalshi:KXWC-26-BRA", base_url="x", include_hyperliquid=True)
    assert cap["path"] == "/v1/markets/kalshi%3AKXWC-26-BRA/related"
    assert resp["related"] == _CANNED_RELATED["related"]  # normal payload intact
    assert resp["hyperliquid_related"] == [_ROWS[0]]  # rows verbatim
    assert "mint-time daily snapshot" in resp["hyperliquid_note"]
    assert "hyperliquid_file_missing" not in resp


async def test_tool_include_hyperliquid_no_match_is_empty_list(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    cap: dict[str, Any] = {}
    _patch_http(monkeypatch, cap)
    _patch_hl_singleton(monkeypatch, _loaded_index(tmp_path))
    resp = await tools.related_markets(
        "kalshi:KXNO-HL-TWIN", base_url="x", include_hyperliquid=True)
    assert resp["hyperliquid_related"] == []
    assert "hyperliquid_note" in resp
    assert "hyperliquid_file_missing" not in resp  # file loaded fine, just no rows


async def test_tool_include_hyperliquid_missing_file_flag(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    cap: dict[str, Any] = {}
    _patch_http(monkeypatch, cap)
    _patch_hl_singleton(monkeypatch, HLRelatedIndex.load(tmp_path / "nope.jsonl.gz"))
    resp = await tools.related_markets(
        "kalshi:KXWC-26-BRA", base_url="x", include_hyperliquid=True)
    assert resp["hyperliquid_related"] == []
    assert resp["hyperliquid_file_missing"] is True


async def test_tool_default_response_unchanged(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """include_hyperliquid=False (the default) must be byte-identical to today."""
    cap: dict[str, Any] = {}
    _patch_http(monkeypatch, cap)
    _patch_hl_singleton(monkeypatch, _loaded_index(tmp_path))
    baseline = await tools.related_markets("kalshi:KXWC-26-BRA", base_url="x")
    assert baseline == _CANNED_RELATED  # whole-payload equality, not just keys
    assert set(baseline.keys()) == set(_CANNED_RELATED.keys())
    for key in baseline:
        assert not key.startswith("hyperliquid")


async def test_tool_ref_error_short_circuits_before_hl(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    cap: dict[str, Any] = {}
    _patch_http(monkeypatch, cap)
    _patch_hl_singleton(monkeypatch, _loaded_index(tmp_path))
    resp = await tools.related_markets("", base_url="x", include_hyperliquid=True)
    assert resp["error"] == "invalid_market_ref"
    assert "hyperliquid_related" not in resp
    assert "path" not in cap  # never hit the HTTP layer
