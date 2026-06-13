"""Unit tests for pytheum.registry.RouterRegistry.

Covers: add(), replace-on-duplicate, build_router(), openapi_paths(), len().
"""
from __future__ import annotations

from typing import Any

import pytest

from pytheum.registry import RouterRegistry, RouteSpec

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _stub(query: dict[str, str]) -> tuple[int, dict[str, Any]]:
    return 200, {"stub": True}


async def _stub_ref(ref: str, query: dict[str, str]) -> tuple[int, dict[str, Any]]:
    return 200, {"ref": ref}


async def _handler_v1(ref: str, query: dict[str, str]) -> tuple[int, dict[str, Any]]:
    return 200, {"version": "v1"}


async def _handler_v2(ref: str, query: dict[str, str]) -> tuple[int, dict[str, Any]]:
    return 200, {"version": "v2"}


# ---------------------------------------------------------------------------
# Basic add / build_router
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_add_and_dispatch_simple_route() -> None:
    reg = RouterRegistry()
    reg.add(RouteSpec("GET", "/v1/status", _stub, summary="health"))
    router = reg.build_router()
    result = await router.dispatch("GET", "/v1/status", {})
    assert result == (200, {"stub": True})


@pytest.mark.asyncio
async def test_add_and_dispatch_path_param_route() -> None:
    reg = RouterRegistry()
    reg.add(RouteSpec("GET", "/v1/markets/{ref}/ohlcv", _stub_ref))
    router = reg.build_router()
    result = await router.dispatch("GET", "/v1/markets/kalshi:FED-25JUN/ohlcv", {})
    assert result == (200, {"ref": "kalshi:FED-25JUN"})


# ---------------------------------------------------------------------------
# Replace-on-duplicate: last registration wins
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_replace_on_duplicate_last_wins() -> None:
    """Critical: pit overrides serve's ohlcv handler by re-registering the route."""
    reg = RouterRegistry()
    reg.add(RouteSpec("GET", "/v1/markets/{ref}/ohlcv", _handler_v1, summary="ohlcv v1"))
    reg.add(RouteSpec("GET", "/v1/markets/{ref}/ohlcv", _handler_v2, summary="ohlcv v2"))

    router = reg.build_router()
    result = await router.dispatch("GET", "/v1/markets/pm:abc123/ohlcv", {})
    assert result == (200, {"version": "v2"})


def test_replace_on_duplicate_registry_len_stays_one() -> None:
    """Duplicate registration must not grow the registry."""
    reg = RouterRegistry()
    reg.add(RouteSpec("GET", "/v1/markets/{ref}/ohlcv", _handler_v1))
    reg.add(RouteSpec("GET", "/v1/markets/{ref}/ohlcv", _handler_v2))
    assert len(reg) == 1


def test_replace_on_duplicate_summary_is_updated() -> None:
    """The summary from the latest registration is the one that survives."""
    reg = RouterRegistry()
    reg.add(RouteSpec("GET", "/v1/markets/{ref}/ohlcv", _handler_v1, summary="original"))
    reg.add(RouteSpec("GET", "/v1/markets/{ref}/ohlcv", _handler_v2, summary="override"))
    assert reg.routes()[0].summary == "override"


def test_different_methods_are_separate_keys() -> None:
    """GET and POST on the same pattern are independent registrations."""
    reg = RouterRegistry()
    reg.add(RouteSpec("GET", "/v1/status", _stub))
    reg.add(RouteSpec("POST", "/v1/status", _stub))
    assert len(reg) == 2


# ---------------------------------------------------------------------------
# openapi_paths()
# ---------------------------------------------------------------------------


def test_openapi_paths_contains_registered_routes() -> None:
    reg = RouterRegistry()
    reg.add(RouteSpec("GET", "/v1/status", _stub, summary="health check", tags=["meta"]))
    paths = reg.openapi_paths()
    assert "/v1/status" in paths
    assert "get" in paths["/v1/status"]
    op = paths["/v1/status"]["get"]
    assert op["summary"] == "health check"
    assert op["tags"] == ["meta"]


def test_openapi_paths_path_params_in_parameters() -> None:
    reg = RouterRegistry()
    reg.add(RouteSpec("GET", "/v1/markets/{ref}/ohlcv", _stub_ref))
    paths = reg.openapi_paths()
    params = paths["/v1/markets/{ref}/ohlcv"]["get"]["parameters"]
    path_params = [p for p in params if p["in"] == "path"]
    assert any(p["name"] == "ref" and p["required"] is True for p in path_params)


def test_openapi_paths_query_params_in_parameters() -> None:
    reg = RouterRegistry()
    reg.add(RouteSpec(
        "GET", "/v1/markets/matched", _stub,
        params={"league": "Filter by league", "limit": "Max results"},
    ))
    paths = reg.openapi_paths()
    params = paths["/v1/markets/matched"]["get"]["parameters"]
    query_names = {p["name"] for p in params if p["in"] == "query"}
    assert query_names == {"league", "limit"}


def test_openapi_paths_has_200_response() -> None:
    reg = RouterRegistry()
    reg.add(RouteSpec("GET", "/v1/status", _stub))
    paths = reg.openapi_paths()
    assert "200" in paths["/v1/status"]["get"]["responses"]


# ---------------------------------------------------------------------------
# repr / len
# ---------------------------------------------------------------------------


def test_repr_shows_route_count() -> None:
    reg = RouterRegistry()
    reg.add(RouteSpec("GET", "/v1/status", _stub))
    reg.add(RouteSpec("GET", "/v1/markets/matched", _stub))
    assert "2" in repr(reg)


def test_empty_registry_len_zero() -> None:
    assert len(RouterRegistry()) == 0
