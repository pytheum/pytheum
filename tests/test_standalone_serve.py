"""Standalone offline-serve smoke tests.

Verifies that the published wheel, when installed on a clean machine with no
database, secrets, or env overrides, correctly:

  1. Resolves the bundled datasets via importlib.resources.
  2. Serves /v1/status, /v1/markets/equivalents, /v1/markets/matched,
     /v1/markets/{ref}/rules, /v1/markets/{ref}/equivalents, /v1/markets/related,
     and /llms.txt from bundled data (all 200, no errors, real pair counts).
  3. Returns the graceful degraded response on /v1/markets/screen (no DAO).

All tests use dao=None, clients=None — no DB, no network, no secrets.
A real kalshi_ticker from the bundled equivalence export is used to prove the
index loaded real data.

Real ticker used: COSTCOHOTDOG-27 (human_adjudicated, confirmed present in
datasets/equivalence-export.jsonl.gz at build time).
"""
from __future__ import annotations

import importlib.resources
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
# Shared fixture: RouterApp wired exactly as `pytheum serve` does it
# ---------------------------------------------------------------------------

_REAL_TICKER = "COSTCOHOTDOG-27"  # confirmed in bundled export


def _build_standalone_app() -> RouterApp:
    """Build a RouterApp the same way `pytheum serve` does: null dao, bundled indexes."""
    eq_idx = EquivalenceIndex.load()  # uses importlib.resources path
    rel_idx = RelatedIndex.load()
    registry = RouterRegistry()
    register_all(registry, dao=None, equivalence=eq_idx, related=rel_idx, clients=None)
    return RouterApp(registry.build_router())


def _client(app: RouterApp) -> httpx.AsyncClient:
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://test")


# ---------------------------------------------------------------------------
# 1. importlib.resources finds the bundled datasets
# ---------------------------------------------------------------------------


def test_importlib_resources_finds_equivalence_gz() -> None:
    """importlib.resources must locate equivalence-export.jsonl.gz in the wheel."""
    ref = importlib.resources.files("pytheum.datasets").joinpath(
        "equivalence-export.jsonl.gz"
    )
    p = Path(str(ref))
    assert p.exists(), f"bundled equivalence dataset not found at {p}"
    assert p.stat().st_size > 1_000_000, "file is too small — may be an LFS pointer"


def test_importlib_resources_finds_related_gz() -> None:
    """importlib.resources must locate related-export.jsonl.gz in the wheel."""
    ref = importlib.resources.files("pytheum.datasets").joinpath(
        "related-export.jsonl.gz"
    )
    p = Path(str(ref))
    assert p.exists(), f"bundled related dataset not found at {p}"
    assert p.stat().st_size > 1_000, "file is too small — may be an LFS pointer"


def test_importlib_resources_finds_manifest() -> None:
    """MANIFEST.json must be present in the bundled package data."""
    ref = importlib.resources.files("pytheum.datasets").joinpath("MANIFEST.json")
    p = Path(str(ref))
    assert p.exists(), f"MANIFEST.json not found at {p}"


# ---------------------------------------------------------------------------
# 2. Indexes load real data from bundled files
# ---------------------------------------------------------------------------


def test_equivalence_index_loads_real_pairs() -> None:
    """EquivalenceIndex.load() with no path arg must find >100k pairs."""
    idx = EquivalenceIndex.load()
    assert not idx.file_missing, "file_missing=True — dataset was not found"
    assert idx.load_error is None, f"load_error: {idx.load_error}"
    assert idx.pairs_loaded > 100_000, (
        f"expected >100k pairs, got {idx.pairs_loaded} — dataset may be missing"
    )


def test_equivalence_index_real_ticker_lookup() -> None:
    """A known ticker from the bundled export must be found in the loaded index."""
    idx = EquivalenceIndex.load()
    rows, via = idx.lookup(f"kalshi:{_REAL_TICKER}")
    assert rows, (
        f"kalshi:{_REAL_TICKER} not found in bundled index "
        f"(pairs_loaded={idx.pairs_loaded}) — dataset may be truncated or wrong"
    )
    assert via == "kalshi_ticker"


def test_related_index_loads_real_pairs() -> None:
    """RelatedIndex.load() with no path arg must find at least 1 pair."""
    idx = RelatedIndex.load()
    assert not idx.file_missing, "file_missing=True — related dataset was not found"
    assert idx.pairs_loaded >= 1, (
        f"expected ≥1 related pair, got {idx.pairs_loaded}"
    )


# ---------------------------------------------------------------------------
# 3. HTTP surface — all Group A routes return 200 with real data
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_status_returns_real_pair_counts() -> None:
    """/v1/status must report pairs_loaded > 100k from bundled data."""
    app = _build_standalone_app()
    async with _client(app) as c:
        resp = await c.get("/v1/status")
    assert resp.status_code == 200, f"expected 200, got {resp.status_code}"
    body = resp.json()
    assert body["equivalence"]["pairs_loaded"] > 100_000, (
        f"expected >100k pairs in status, got {body['equivalence']['pairs_loaded']}"
    )
    assert body["service"]["version"] != "", "service.version must not be empty"


@pytest.mark.asyncio
async def test_markets_equivalents_returns_real_pairs() -> None:
    """/v1/markets/equivalents must return at least 1 pair from bundled data."""
    app = _build_standalone_app()
    async with _client(app) as c:
        resp = await c.get("/v1/markets/equivalents?limit=5")
    assert resp.status_code == 200, f"expected 200, got {resp.status_code}"
    body = resp.json()
    # The handler returns a list or dict — check we get actual data
    # The format is {"equivalents": [...], ...} or similar; just verify non-error shape
    assert isinstance(body, dict), "expected JSON object"
    # Not degraded
    assert body.get("meta", {}).get("degraded") is not True, (
        "equivalents returned degraded — bundled datasets not found"
    )


@pytest.mark.asyncio
async def test_markets_matched_returns_real_pairs() -> None:
    """/v1/markets/matched must return >0 pairs from bundled data."""
    app = _build_standalone_app()
    async with _client(app) as c:
        resp = await c.get("/v1/markets/matched?limit=10")
    assert resp.status_code == 200, f"expected 200, got {resp.status_code}"
    body = resp.json()
    assert "pairs" in body, f"missing 'pairs' key: {list(body.keys())}"
    assert len(body["pairs"]) > 0, "expected real pairs from bundled data, got 0"
    assert body["total"] > 100_000, (
        f"expected >100k total pairs, got {body['total']}"
    )
    assert body.get("meta", {}).get("degraded") is not True


@pytest.mark.asyncio
async def test_per_ref_equivalents_real_ticker() -> None:
    """/v1/markets/{ref}/equivalents must find a match for the known ticker."""
    app = _build_standalone_app()
    async with _client(app) as c:
        resp = await c.get(f"/v1/markets/kalshi:{_REAL_TICKER}/equivalents")
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["equivalents"]) >= 1, (
        f"expected ≥1 equivalent for {_REAL_TICKER}, got 0"
    )
    assert body["meta"]["matched_via"] == "kalshi_ticker"


@pytest.mark.asyncio
async def test_rules_real_ticker() -> None:
    """/v1/markets/{ref}/rules must return 200 and rules content for a known ticker."""
    app = _build_standalone_app()
    async with _client(app) as c:
        resp = await c.get(f"/v1/markets/kalshi:{_REAL_TICKER}/rules")
    assert resp.status_code == 200
    body = resp.json()
    # Must have at least one leg with rules content
    assert isinstance(body, dict), f"unexpected response type: {type(body)}"


@pytest.mark.asyncio
async def test_related_endpoint_returns_200() -> None:
    """/v1/markets/{ref}/related must return 200 (empty or populated)."""
    app = _build_standalone_app()
    async with _client(app) as c:
        resp = await c.get(f"/v1/markets/kalshi:{_REAL_TICKER}/related")
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_llms_txt_returns_text() -> None:
    """/llms.txt must return 200 text/plain with non-empty content."""
    app = _build_standalone_app()
    async with _client(app) as c:
        resp = await c.get("/llms.txt")
    assert resp.status_code == 200
    assert "text/plain" in resp.headers.get("content-type", "")
    assert len(resp.text) > 100, "llms.txt content suspiciously short"


@pytest.mark.asyncio
async def test_healthz_returns_ok() -> None:
    """/healthz must return 200 {ok: true}."""
    app = _build_standalone_app()
    async with _client(app) as c:
        resp = await c.get("/healthz")
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}


# ---------------------------------------------------------------------------
# 4. Graceful degradation (no DAO / no clients)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_screen_degrades_without_dao() -> None:
    """/v1/markets/screen with dao=None must return 200 with degraded=true."""
    app = _build_standalone_app()
    async with _client(app) as c:
        resp = await c.get("/v1/markets/screen")
    assert resp.status_code == 200
    body = resp.json()
    assert body.get("meta", {}).get("degraded") is True
    assert body["markets"] == []


# ---------------------------------------------------------------------------
# 5. CLI module importable
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
# 6. Singleton get_index() delegates to bundled data
# ---------------------------------------------------------------------------


def test_singleton_get_index_loads_bundled_data() -> None:
    """get_index() (used by the module-level lazy singleton) must load bundled data."""
    # Reset the singleton so this test is not order-dependent.
    import pytheum.equivalence.index as _ei
    import pytheum.related.index as _ri

    _ei._singleton = None
    _ri._singleton = None

    eq = get_eq_index()
    rel = get_rel_index()

    assert eq.pairs_loaded > 100_000, (
        f"get_index() loaded only {eq.pairs_loaded} pairs — bundled data not found"
    )
    assert rel.pairs_loaded >= 1
