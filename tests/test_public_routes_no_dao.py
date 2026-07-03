"""Regression: Group A routes + /screen must register unconditionally and
return 200 (not 404 or 500) when dao=None.

This is the open-source standalone correctness requirement: calling
register_all(registry, dao=None, clients=None) must produce a fully-routed
RouterApp whose Group A surface (status / equivalents / matched / rules /
related) and /screen endpoint all serve without crashing, even with no DB.

Root cause found in the 2026-06-13 pre-flip load test (Stage-0 bug):
  - Group A routes were only wired inside _bind_api_router which required
    EMBEDDING_WORKER_ENABLED=true; secretless boots saw 404 for all of them.
  - /screen called dao.screen_markets() unconditionally, 500-ing with dao=None.
"""
from __future__ import annotations

import httpx
import pytest

from pytheum.api import register_all
from pytheum.api.markets_equivalents import _cache as _equivalents_cache
from pytheum.registry import RouterRegistry
from pytheum.routing import Router, RouterApp


def _build_router_no_dao() -> Router:
    """Build a router via register_all with dao=None — the standalone boot path."""
    registry = RouterRegistry()
    register_all(registry, dao=None, clients=None)
    return registry.build_router()


def _client(router: Router) -> httpx.AsyncClient:
    transport = httpx.ASGITransport(app=RouterApp(router))
    return httpx.AsyncClient(transport=transport, base_url="http://test")


# ---------------------------------------------------------------------------
# Group A — all six public-serve routes must return 200 (not 404)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_status_returns_200_without_dao() -> None:
    """GET /v1/status must be registered and return 200 when dao=None."""
    async with _client(_build_router_no_dao()) as client:
        resp = await client.get("/v1/status")
    assert resp.status_code == 200, f"expected 200, got {resp.status_code}"
    body = resp.json()
    assert "service" in body


@pytest.mark.asyncio
async def test_equivalents_collection_returns_200_without_dao() -> None:
    """GET /v1/markets/equivalents must be registered and return 200 when dao=None."""
    # Clear the module-level equivalents cache so a prior test's empty result
    # (if any) does not mask a crash in the handler.
    _equivalents_cache.clear()
    async with _client(_build_router_no_dao()) as client:
        resp = await client.get("/v1/markets/equivalents")
    assert resp.status_code == 200, f"expected 200, got {resp.status_code}"


@pytest.mark.asyncio
async def test_matched_returns_200_without_dao() -> None:
    """GET /v1/markets/matched must be registered and return 200 when dao=None."""
    async with _client(_build_router_no_dao()) as client:
        resp = await client.get("/v1/markets/matched")
    assert resp.status_code == 200, f"expected 200, got {resp.status_code}"
    body = resp.json()
    assert "pairs" in body


@pytest.mark.asyncio
async def test_per_ref_equivalents_returns_200_without_dao() -> None:
    """GET /v1/markets/{ref}/equivalents must be registered and return 200 when dao=None."""
    async with _client(_build_router_no_dao()) as client:
        resp = await client.get("/v1/markets/kalshi:PRES-2024-DJT/equivalents")
    assert resp.status_code == 200, f"expected 200, got {resp.status_code}"


@pytest.mark.asyncio
async def test_rules_returns_200_without_dao() -> None:
    """GET /v1/markets/{ref}/rules must be registered and return 200 when dao=None."""
    async with _client(_build_router_no_dao()) as client:
        resp = await client.get("/v1/markets/kalshi:PRES-2024-DJT/rules")
    assert resp.status_code == 200, f"expected 200, got {resp.status_code}"


@pytest.mark.asyncio
async def test_related_returns_200_without_dao() -> None:
    """GET /v1/markets/{ref}/related must be registered and return 200 when dao=None."""
    async with _client(_build_router_no_dao()) as client:
        resp = await client.get("/v1/markets/kalshi:PRES-2024-DJT/related")
    assert resp.status_code == 200, f"expected 200, got {resp.status_code}"


# ---------------------------------------------------------------------------
# Group B — /screen must return 200 (not 500) with dao=None
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_screen_returns_200_without_dao() -> None:
    """GET /v1/markets/screen must not 500 when dao=None (blocker 2 fix)."""
    async with _client(_build_router_no_dao()) as client:
        resp = await client.get("/v1/markets/screen")
    assert resp.status_code == 200, f"expected 200, got {resp.status_code}"
    body = resp.json()
    assert body.get("meta", {}).get("degraded") is True
    assert body["markets"] == []
    assert body["count"] == 0


@pytest.mark.asyncio
async def test_screen_degraded_reason_is_db_unavailable() -> None:
    """Degraded /screen response must carry a degraded_reason field."""
    async with _client(_build_router_no_dao()) as client:
        resp = await client.get("/v1/markets/screen")
    body = resp.json()
    assert body["meta"]["degraded_reason"] == "db_unavailable"


@pytest.mark.asyncio
async def test_search_returns_200_without_dao() -> None:
    """GET /v1/markets/search must register (before /{ref}) and degrade to 200,
    not 500, when dao=None. With a query it reports db_unavailable; without a
    query it reports the missing_query error — both 200."""
    async with _client(_build_router_no_dao()) as client:
        resp = await client.get("/v1/markets/search?q=super+bowl")
        resp_noq = await client.get("/v1/markets/search")
    assert resp.status_code == 200, f"expected 200, got {resp.status_code}"
    assert resp.json()["meta"]["degraded_reason"] == "db_unavailable"
    assert resp_noq.status_code == 200
    assert resp_noq.json()["meta"]["error"] == "missing_query"


# --- RouterApp overrides (llms_txt + text_routes) — the :8445 wrapper-process fix ---

async def _collect_response(app, path):
    sent = []
    async def send(msg): sent.append(msg)
    async def receive(): return {"type": "http.request"}
    await app({"type": "http", "method": "GET", "path": path, "query_string": b""}, receive, send)
    status = next(m["status"] for m in sent if m["type"] == "http.response.start")
    body = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    ctype = dict(next(m for m in sent if m["type"] == "http.response.start")["headers"])
    return status, body.decode(), ctype.get(b"content-type", b"").decode()


def test_routerapp_llms_override_served():
    import asyncio
    from pytheum.routing import Router, RouterApp
    app = RouterApp(Router(), llms_txt="BASE\n## EXTRA SECTION")
    status, body, ctype = asyncio.run(_collect_response(app, "/llms.txt"))
    assert status == 200 and "EXTRA SECTION" in body and "text/plain" in ctype


def test_routerapp_llms_default_unchanged():
    import asyncio
    from pytheum.llms_txt import LLMS_TXT
    from pytheum.routing import Router, RouterApp
    app = RouterApp(Router())
    status, body, _ = asyncio.run(_collect_response(app, "/llms.txt"))
    assert status == 200 and body == LLMS_TXT


def test_routerapp_text_route_served_and_error_safe():
    import asyncio
    from pytheum.routing import Router, RouterApp
    app = RouterApp(Router(), text_routes={"/v1/stream/metrics": lambda: "metric_a 1\n"})
    status, body, ctype = asyncio.run(_collect_response(app, "/v1/stream/metrics"))
    assert status == 200 and body == "metric_a 1\n" and "text/plain" in ctype
    def boom() -> str: raise RuntimeError("x")
    app2 = RouterApp(Router(), text_routes={"/v1/stream/metrics": boom})
    status2, body2, _ = asyncio.run(_collect_response(app2, "/v1/stream/metrics"))
    assert status2 == 500 and "text_route_failed" in body2
