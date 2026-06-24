"""Standalone offline-serve smoke tests.

The matcher gold set is NO LONGER shipped in the package — it is served for free
via the hosted API, loaded at runtime from a private path (PYTHEUM_EQUIVALENCE_PATH
/ PYTHEUM_RELATED_PATH). A clean `pip install pytheum` therefore ships NO dataset
blobs; only MANIFEST.json remains as metadata.

These tests verify that with no dataset configured the stack:

  1. Resolves NO bundled .gz via importlib.resources (only MANIFEST.json is present).
  2. Degrades gracefully — EquivalenceIndex.load()/RelatedIndex.load() with no path
     return an EMPTY index with file_missing=True, never crashing.
  3. Still serves /v1/status, /v1/markets/equivalents, /v1/markets/matched,
     /v1/markets/{ref}/rules, /v1/markets/{ref}/equivalents, /v1/markets/related,
     /llms.txt, and /healthz (all 200) — and degrades /v1/markets/screen (no DAO).
  4. Serves real pairs when pointed at an explicit dataset file (the runtime
     contract): RouterApp wiring works end-to-end over a tiny synthetic fixture.

All tests use dao=None, clients=None — no DB, no network, no secrets.
"""
from __future__ import annotations

import gzip
import importlib.resources
import json
from pathlib import Path

import httpx
import pytest

from pytheum.api import register_all
from pytheum.equivalence.index import EquivalenceIndex
from pytheum.equivalence.index import get_index as get_eq_index
from pytheum.registry import RouterRegistry
from pytheum.related.index import RelatedIndex
from pytheum.related.index import get_index as get_rel_index
from pytheum.routing import RouterApp

# ---------------------------------------------------------------------------
# Synthetic fixtures: a tiny dataset written to tmp_path, loaded via an explicit
# path. This stands in for the private runtime dataset (PYTHEUM_*_PATH) without
# reintroducing a dependency on a large tracked file.
# ---------------------------------------------------------------------------

_FIXTURE_TICKER = "FIXTURETESTMKT-27"


def _write_equivalence_fixture(tmp_path: Path) -> Path:
    """Write a few synthetic equivalence rows, gzipped, and return the path."""
    rows = [
        {
            "kalshi_ticker": _FIXTURE_TICKER,
            "kalshi_ref": f"kalshi:{_FIXTURE_TICKER}",
            "kalshi_title": "Fixture market",
            "pm_ref": "polymarket:12345",
            "pm_gamma_id": "12345",
            "pm_condition_id": "0xabc123",
            "pm_slug": "fixture-market",
            "pm_title": "Fixture market (PM)",
            "bet_type": "moneyline",
            "method": "human_adjudicated",
        },
        {
            "kalshi_ticker": "FIXTURETESTMKT-28",
            "kalshi_ref": "kalshi:FIXTURETESTMKT-28",
            "kalshi_title": "Second fixture market",
            "pm_ref": "polymarket:67890",
            "pm_gamma_id": "67890",
            "pm_condition_id": "0xdef456",
            "pm_slug": "second-fixture-market",
            "pm_title": "Second fixture market (PM)",
            "bet_type": "total",
            "method": "opus_backstop",
        },
    ]
    p = tmp_path / "equivalence-export.jsonl.gz"
    with gzip.open(p, "wt", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row) + "\n")
    return p


def _write_related_fixture(tmp_path: Path) -> Path:
    """Write a single synthetic related row, gzipped, and return the path."""
    rows = [
        {
            "kalshi_ticker": _FIXTURE_TICKER,
            "kalshi_title": "Fixture market",
            "pm_gamma_id": "12345",
            "pm_slug": "fixture-market",
            "pm_title": "Fixture market (PM)",
            "relation": "same_asset_date_contained",
        },
    ]
    p = tmp_path / "related-export.jsonl.gz"
    with gzip.open(p, "wt", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row) + "\n")
    return p


def _build_app_with_fixtures(tmp_path: Path) -> RouterApp:
    """Build a RouterApp the way `pytheum serve` does, but with synthetic indexes
    loaded from explicit tmp_path files (stand-in for the private runtime dataset)."""
    eq_idx = EquivalenceIndex.load(_write_equivalence_fixture(tmp_path))
    rel_idx = RelatedIndex.load(_write_related_fixture(tmp_path))
    registry = RouterRegistry()
    register_all(registry, dao=None, equivalence=eq_idx, related=rel_idx, clients=None)
    return RouterApp(registry.build_router())


def _build_empty_app() -> RouterApp:
    """Build a RouterApp with NO dataset configured — indexes degrade to empty."""
    eq_idx = EquivalenceIndex.load()  # no path, no bundled data -> empty
    rel_idx = RelatedIndex.load()
    registry = RouterRegistry()
    register_all(registry, dao=None, equivalence=eq_idx, related=rel_idx, clients=None)
    return RouterApp(registry.build_router())


def _client(app: RouterApp) -> httpx.AsyncClient:
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://test")


# ---------------------------------------------------------------------------
# 1. The package ships NO dataset .gz — only MANIFEST.json remains
# ---------------------------------------------------------------------------


def test_no_bundled_equivalence_gz() -> None:
    """The matcher gold set must NOT ship in the package — no bundled .gz."""
    ref = importlib.resources.files("pytheum.datasets").joinpath(
        "equivalence-export.jsonl.gz"
    )
    assert not Path(str(ref)).exists(), (
        "equivalence-export.jsonl.gz is bundled in the package — the gold set must "
        "be served via the API, not shipped as a download"
    )


def test_no_bundled_related_gz() -> None:
    """The related dataset must NOT ship in the package — no bundled .gz."""
    ref = importlib.resources.files("pytheum.datasets").joinpath(
        "related-export.jsonl.gz"
    )
    assert not Path(str(ref)).exists(), "related-export.jsonl.gz must not be bundled"


def test_manifest_still_present() -> None:
    """MANIFEST.json (metadata, not data) must still be present in package data."""
    ref = importlib.resources.files("pytheum.datasets").joinpath("MANIFEST.json")
    p = Path(str(ref))
    assert p.exists(), f"MANIFEST.json not found at {p}"


# ---------------------------------------------------------------------------
# 2. Indexes degrade gracefully when no dataset is configured
# ---------------------------------------------------------------------------


def test_equivalence_index_degrades_when_unconfigured() -> None:
    """EquivalenceIndex.load() with no path + no bundled data must degrade EMPTY."""
    idx = EquivalenceIndex.load()
    assert idx.file_missing, "expected file_missing=True with no dataset configured"
    assert idx.pairs_loaded == 0, (
        f"expected 0 pairs with no dataset, got {idx.pairs_loaded}"
    )


def test_related_index_degrades_when_unconfigured() -> None:
    """RelatedIndex.load() with no path + no bundled data must degrade EMPTY."""
    idx = RelatedIndex.load()
    assert idx.file_missing, "expected file_missing=True with no dataset configured"
    assert idx.pairs_loaded == 0, (
        f"expected 0 pairs with no dataset, got {idx.pairs_loaded}"
    )


# ---------------------------------------------------------------------------
# 3. Indexes load real data when pointed at an explicit dataset file
# ---------------------------------------------------------------------------


def test_equivalence_index_loads_from_explicit_path(tmp_path: Path) -> None:
    """EquivalenceIndex.load(path) must ingest a real .jsonl.gz from disk."""
    idx = EquivalenceIndex.load(_write_equivalence_fixture(tmp_path))
    assert not idx.file_missing
    assert idx.load_error is None, f"load_error: {idx.load_error}"
    assert idx.pairs_loaded == 2
    rows, via = idx.lookup(f"kalshi:{_FIXTURE_TICKER}")
    assert rows, f"kalshi:{_FIXTURE_TICKER} not found in loaded index"
    assert via == "kalshi_ticker"


def test_related_index_loads_from_explicit_path(tmp_path: Path) -> None:
    """RelatedIndex.load(path) must ingest a real .jsonl.gz from disk."""
    idx = RelatedIndex.load(_write_related_fixture(tmp_path))
    assert not idx.file_missing
    assert idx.pairs_loaded == 1


# ---------------------------------------------------------------------------
# 4. HTTP surface — routes return 200 with real data from the configured dataset
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_status_returns_pair_counts(tmp_path: Path) -> None:
    """/v1/status must report the configured dataset's pairs_loaded."""
    import pytheum.api.status as _status_mod

    _status_mod._cache = None  # 60s status cache is module-level; clear for isolation
    app = _build_app_with_fixtures(tmp_path)
    async with _client(app) as c:
        resp = await c.get("/v1/status")
    assert resp.status_code == 200, f"expected 200, got {resp.status_code}"
    body = resp.json()
    assert body["equivalence"]["pairs_loaded"] == 2, (
        f"expected 2 pairs in status, got {body['equivalence']['pairs_loaded']}"
    )
    assert body["service"]["version"] != "", "service.version must not be empty"


@pytest.mark.asyncio
async def test_markets_equivalents_returns_pairs(tmp_path: Path) -> None:
    """/v1/markets/equivalents must return data (not degraded) from the dataset."""
    app = _build_app_with_fixtures(tmp_path)
    async with _client(app) as c:
        resp = await c.get("/v1/markets/equivalents?limit=5")
    assert resp.status_code == 200, f"expected 200, got {resp.status_code}"
    body = resp.json()
    assert isinstance(body, dict), "expected JSON object"
    assert body.get("meta", {}).get("degraded") is not True, (
        "equivalents returned degraded — configured dataset not found"
    )


@pytest.mark.asyncio
async def test_markets_matched_returns_pairs(tmp_path: Path) -> None:
    """/v1/markets/matched must return the configured dataset's pairs."""
    app = _build_app_with_fixtures(tmp_path)
    async with _client(app) as c:
        resp = await c.get("/v1/markets/matched?limit=10")
    assert resp.status_code == 200, f"expected 200, got {resp.status_code}"
    body = resp.json()
    assert "pairs" in body, f"missing 'pairs' key: {list(body.keys())}"
    assert len(body["pairs"]) > 0, "expected pairs from configured dataset, got 0"
    assert body["total"] == 2, f"expected 2 total pairs, got {body['total']}"
    assert body.get("meta", {}).get("degraded") is not True


@pytest.mark.asyncio
async def test_per_ref_equivalents_fixture_ticker(tmp_path: Path) -> None:
    """/v1/markets/{ref}/equivalents must find a match for the fixture ticker."""
    app = _build_app_with_fixtures(tmp_path)
    async with _client(app) as c:
        resp = await c.get(f"/v1/markets/kalshi:{_FIXTURE_TICKER}/equivalents")
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["equivalents"]) >= 1, (
        f"expected ≥1 equivalent for {_FIXTURE_TICKER}, got 0"
    )
    assert body["meta"]["matched_via"] == "kalshi_ticker"


@pytest.mark.asyncio
async def test_rules_fixture_ticker(tmp_path: Path) -> None:
    """/v1/markets/{ref}/rules must return 200 for a known ticker."""
    app = _build_app_with_fixtures(tmp_path)
    async with _client(app) as c:
        resp = await c.get(f"/v1/markets/kalshi:{_FIXTURE_TICKER}/rules")
    assert resp.status_code == 200
    body = resp.json()
    assert isinstance(body, dict), f"unexpected response type: {type(body)}"


@pytest.mark.asyncio
async def test_related_endpoint_returns_200(tmp_path: Path) -> None:
    """/v1/markets/{ref}/related must return 200 (empty or populated)."""
    app = _build_app_with_fixtures(tmp_path)
    async with _client(app) as c:
        resp = await c.get(f"/v1/markets/kalshi:{_FIXTURE_TICKER}/related")
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_llms_txt_returns_text(tmp_path: Path) -> None:
    """/llms.txt must return 200 text/plain with non-empty content."""
    app = _build_app_with_fixtures(tmp_path)
    async with _client(app) as c:
        resp = await c.get("/llms.txt")
    assert resp.status_code == 200
    assert "text/plain" in resp.headers.get("content-type", "")
    assert len(resp.text) > 100, "llms.txt content suspiciously short"


@pytest.mark.asyncio
async def test_healthz_returns_ok(tmp_path: Path) -> None:
    """/healthz must return 200 {ok: true}."""
    app = _build_app_with_fixtures(tmp_path)
    async with _client(app) as c:
        resp = await c.get("/healthz")
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}


# ---------------------------------------------------------------------------
# 5. HTTP surface still serves (degraded but 200) when no dataset is configured
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_status_returns_zero_pairs_when_unconfigured() -> None:
    """/v1/status must report 0 pairs and still return 200 when no dataset is set."""
    import pytheum.api.status as _status_mod

    _status_mod._cache = None  # 60s status cache is module-level; clear for isolation
    app = _build_empty_app()
    async with _client(app) as c:
        resp = await c.get("/v1/status")
    assert resp.status_code == 200
    body = resp.json()
    assert body["equivalence"]["pairs_loaded"] == 0


@pytest.mark.asyncio
async def test_healthz_ok_when_unconfigured() -> None:
    """/healthz must return 200 {ok: true} even with no dataset configured."""
    app = _build_empty_app()
    async with _client(app) as c:
        resp = await c.get("/healthz")
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}


# ---------------------------------------------------------------------------
# 6. Graceful degradation (no DAO / no clients)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_screen_degrades_without_dao(tmp_path: Path) -> None:
    """/v1/markets/screen with dao=None must return 200 with degraded=true."""
    app = _build_app_with_fixtures(tmp_path)
    async with _client(app) as c:
        resp = await c.get("/v1/markets/screen")
    assert resp.status_code == 200
    body = resp.json()
    assert body.get("meta", {}).get("degraded") is True
    assert body["markets"] == []


# ---------------------------------------------------------------------------
# 7. CLI module importable
# ---------------------------------------------------------------------------


def test_cli_main_importable() -> None:
    """pytheum.cli.main must be importable — entry-point integrity check."""
    from pytheum.cli import main  # noqa: F401
    assert callable(main)


def test_cli_serve_help_exits_0(capsys: pytest.CaptureFixture[str]) -> None:
    """pytheum serve --help must exit 0 without crashing."""

    from pytheum.cli import main

    with pytest.raises(SystemExit) as exc_info:
        main(["serve", "--help"])
    assert exc_info.value.code == 0


# ---------------------------------------------------------------------------
# 8. Singleton get_index() degrades gracefully without a configured dataset
# ---------------------------------------------------------------------------


def test_singleton_get_index_degrades_when_unconfigured() -> None:
    """get_index() (module-level lazy singleton) must degrade to an empty index."""
    # Reset the singleton so this test is not order-dependent.
    import pytheum.equivalence.index as _ei
    import pytheum.related.index as _ri

    _ei._singleton = None
    _ri._singleton = None

    eq = get_eq_index()
    rel = get_rel_index()

    assert eq.pairs_loaded == 0, (
        f"get_index() loaded {eq.pairs_loaded} pairs — expected 0 (no dataset shipped)"
    )
    assert rel.pairs_loaded == 0
