"""Unit tests for pytheum.routing.RouterApp over httpx.ASGITransport.

Adapted from pytheum-stream tests/unit/test_asgi_app.py.
"""
from __future__ import annotations

from decimal import Decimal
from typing import Any

import httpx
import pytest

from pytheum.routing import Router, RouterApp


def _client(router: Router) -> httpx.AsyncClient:
    transport = httpx.ASGITransport(app=RouterApp(router))
    return httpx.AsyncClient(transport=transport, base_url="http://test")


@pytest.mark.asyncio
async def test_dispatch_route_with_path_param_and_query() -> None:
    router = Router()

    async def handler(ref: str, query: dict[str, str]) -> Any:
        return 200, {"ref": ref, "limit": query.get("limit")}

    router.add("GET", "/v1/markets/{ref}/context", handler)
    async with _client(router) as client:
        resp = await client.get("/v1/markets/polymarket:30615/context?limit=5")
    assert resp.status_code == 200
    assert resp.json() == {"ref": "polymarket:30615", "limit": "5"}


@pytest.mark.asyncio
async def test_unknown_path_is_json_404() -> None:
    async with _client(Router()) as client:
        resp = await client.get("/v1/markets/nope")
    assert resp.status_code == 404
    assert resp.json() == {"detail": "not found"}


@pytest.mark.asyncio
async def test_handler_exception_is_json_500_not_disconnect() -> None:
    router = Router()

    async def boom(query: dict[str, str]) -> Any:
        raise RuntimeError("kaboom")

    router.add("GET", "/v1/markets/screen", boom)
    async with _client(router) as client:
        resp = await client.get("/v1/markets/screen")
    assert resp.status_code == 500
    assert resp.json() == {"error": "internal_error"}


@pytest.mark.asyncio
async def test_decimal_values_serialize_as_floats() -> None:
    router = Router()

    async def handler(query: dict[str, str]) -> Any:
        return 200, {"implied_yes": Decimal("0.1645")}

    router.add("GET", "/v1/markets/screen", handler)
    async with _client(router) as client:
        resp = await client.get("/v1/markets/screen")
    assert resp.status_code == 200
    assert resp.json() == {"implied_yes": 0.1645}


@pytest.mark.asyncio
async def test_healthz_builtin() -> None:
    async with _client(Router()) as client:
        resp = await client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}


@pytest.mark.asyncio
async def test_llms_txt_builtin() -> None:
    async with _client(Router()) as client:
        resp = await client.get("/llms.txt")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/plain")
    assert "Pytheum" in resp.text
