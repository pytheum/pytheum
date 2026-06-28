"""GET /v1/about and GET /v1/guide — no-install REST mirrors of t_about / t_guide.

These endpoints serve the exact payloads produced by
``pytheum.mcp.guide.agent_about`` / ``agent_guide`` over plain HTTP, so an agent
needs no MCP install to read who Pytheum is and how to drive the API.
"""
from __future__ import annotations

import httpx
import pytest

from pytheum.api import register_all
from pytheum.api.meta import handle_about, handle_guide
from pytheum.mcp.guide import ACCESS_NOTE
from pytheum.registry import RouterRegistry
from pytheum.routing import Router, RouterApp


def _build_router_no_dao() -> Router:
    registry = RouterRegistry()
    register_all(registry, dao=None, clients=None)
    return registry.build_router()


def _client(router: Router) -> httpx.AsyncClient:
    transport = httpx.ASGITransport(app=RouterApp(router))
    return httpx.AsyncClient(transport=transport, base_url="http://test")


# ---------------------------------------------------------------------------
# Handler-level
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_handle_about_returns_200_with_expected_keys() -> None:
    code, body = await handle_about({})
    assert code == 200
    assert body["name"] == "Pytheum"
    assert "founders" in body and len(body["founders"]) >= 2
    assert "links" in body
    assert body["access"] == ACCESS_NOTE


@pytest.mark.asyncio
async def test_handle_guide_returns_200_with_tools_and_access() -> None:
    code, body = await handle_guide({})
    assert code == 200
    assert body["service"] == "pytheum"
    assert body["access"] == ACCESS_NOTE
    tool_names = {t["name"] for grp in body["tool_groups"] for t in grp["tools"]}
    assert "t_status" in tool_names
    assert "t_guide" in tool_names


def test_access_note_has_no_em_dash() -> None:
    assert "—" not in ACCESS_NOTE
    assert "mcp.pytheum.com/v1/" in ACCESS_NOTE


# ---------------------------------------------------------------------------
# Routed (registered + dispatched) — must serve 200 with dao=None
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_about_route_returns_200_without_dao() -> None:
    async with _client(_build_router_no_dao()) as client:
        resp = await client.get("/v1/about")
    assert resp.status_code == 200
    body = resp.json()
    assert "founders" in body
    assert "links" in body
    assert body["access"] == ACCESS_NOTE


@pytest.mark.asyncio
async def test_guide_route_returns_200_without_dao() -> None:
    async with _client(_build_router_no_dao()) as client:
        resp = await client.get("/v1/guide")
    assert resp.status_code == 200
    body = resp.json()
    assert "tool_groups" in body
    assert body["access"] == ACCESS_NOTE
    tool_names = {t["name"] for grp in body["tool_groups"] for t in grp["tools"]}
    assert "t_status" in tool_names
