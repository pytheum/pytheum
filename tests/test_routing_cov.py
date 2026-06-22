"""Coverage tests for pytheum.routing — Router, RouterApp ASGI, serve_embedded."""
from __future__ import annotations

import asyncio
import json
from decimal import Decimal
from typing import Any

import pytest

from pytheum.routing import (
    DecimalEncoder,
    Router,
    RouterApp,
    _EmbeddedServer,
    serve_embedded,
)

# ---------------------------------------------------------------------------
# ASGI helpers
# ---------------------------------------------------------------------------


async def _call(
    app: RouterApp,
    *,
    method: str = "GET",
    path: str = "/",
    query: bytes = b"",
) -> tuple[int, dict[bytes, bytes], bytes]:
    """Drive a RouterApp over a minimal ASGI http cycle; return status, headers, body."""
    scope: dict[str, Any] = {
        "type": "http",
        "method": method,
        "path": path,
        "query_string": query,
    }
    sent: list[dict[str, Any]] = []

    async def receive() -> dict[str, Any]:  # pragma: no cover - not used for http
        return {"type": "http.request"}

    async def send(msg: dict[str, Any]) -> None:
        sent.append(msg)

    await app(scope, receive, send)
    start = next(m for m in sent if m["type"] == "http.response.start")
    body = b"".join(
        m.get("body", b"") for m in sent if m["type"] == "http.response.body"
    )
    headers = {k: v for k, v in start["headers"]}
    return start["status"], headers, body


# ---------------------------------------------------------------------------
# Router (sync + async handlers, path params, query parsing)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_router_async_handler_awaited() -> None:
    async def h(qs: dict[str, str]) -> tuple[int, dict[str, str]]:
        return 200, {"async": "yes"}

    r = Router()
    r.add("GET", "/v1/status", h)
    out = await r.dispatch("GET", "/v1/status", {})
    assert out == (200, {"async": "yes"})


@pytest.mark.asyncio
async def test_router_sync_handler_returned_directly() -> None:
    def h(qs: dict[str, str]) -> tuple[int, dict[str, str]]:
        return 200, {"sync": "yes"}

    r = Router()
    r.add("GET", "/v1/status", h)
    out = await r.dispatch("GET", "/v1/status", {})
    assert out == (200, {"sync": "yes"})


@pytest.mark.asyncio
async def test_router_multi_segment_params_decoded() -> None:
    def h(a: str, b: str, qs: dict[str, str]) -> dict[str, str]:
        return {"a": a, "b": b}

    r = Router()
    r.add("GET", "/v1/{a}/sub/{b}", h)
    out = await r.dispatch("GET", "/v1/x%20y/sub/z", {})
    assert out == {"a": "x y", "b": "z"}


@pytest.mark.asyncio
async def test_router_method_case_insensitive() -> None:
    def h(qs: dict[str, str]) -> str:
        return "ok"

    r = Router()
    r.add("get", "/v1/status", h)
    assert await r.dispatch("GET", "/v1/status", {}) == "ok"


# ---------------------------------------------------------------------------
# DecimalEncoder
# ---------------------------------------------------------------------------


def test_decimal_encoder_converts_decimal() -> None:
    out = json.dumps({"price": Decimal("1.5")}, cls=DecimalEncoder)
    assert json.loads(out) == {"price": 1.5}


def test_decimal_encoder_falls_back_for_unknown() -> None:
    with pytest.raises(TypeError):
        json.dumps({"x": object()}, cls=DecimalEncoder)


# ---------------------------------------------------------------------------
# RouterApp ASGI surface
# ---------------------------------------------------------------------------


def _app_with(route_method: str = "GET", **kwargs: Any) -> RouterApp:
    r = Router()
    return RouterApp(r)


@pytest.mark.asyncio
async def test_app_healthz() -> None:
    app = RouterApp(Router())
    status, _h, body = await _call(app, path="/healthz")
    assert status == 200
    assert json.loads(body) == {"ok": True}


@pytest.mark.asyncio
async def test_app_llms_txt_plaintext() -> None:
    app = RouterApp(Router())
    status, headers, body = await _call(app, path="/llms.txt")
    assert status == 200
    assert b"text/plain" in headers[b"content-type"]
    assert len(body) > 0


@pytest.mark.asyncio
async def test_app_llms_txt_trailing_slash() -> None:
    app = RouterApp(Router())
    status, _h, _b = await _call(app, path="/llms.txt/")
    assert status == 200


@pytest.mark.asyncio
async def test_app_404_when_no_route() -> None:
    app = RouterApp(Router())
    status, _h, body = await _call(app, path="/nope")
    assert status == 404
    assert json.loads(body) == {"detail": "not found"}


@pytest.mark.asyncio
async def test_app_dispatches_and_parses_query() -> None:
    def h(qs: dict[str, str]) -> tuple[int, dict[str, Any]]:
        return 200, {"qs": qs}

    r = Router()
    r.add("GET", "/v1/status", h)
    app = RouterApp(r)
    status, headers, body = await _call(
        app, path="/v1/status", query=b"limit=5&x=y"
    )
    assert status == 200
    assert headers[b"content-type"] == b"application/json"
    assert json.loads(body) == {"qs": {"limit": "5", "x": "y"}}


@pytest.mark.asyncio
async def test_app_handler_exception_returns_500() -> None:
    def h(qs: dict[str, str]) -> tuple[int, dict[str, Any]]:
        raise RuntimeError("boom")

    r = Router()
    r.add("GET", "/v1/boom", h)
    app = RouterApp(r)
    status, _h, body = await _call(app, path="/v1/boom")
    assert status == 500
    assert json.loads(body) == {"error": "internal_error"}


@pytest.mark.asyncio
async def test_app_timeout_returns_503(monkeypatch: pytest.MonkeyPatch) -> None:
    import pytheum.routing as routing_mod

    # Shrink the dispatch timeout so the test is instant.
    monkeypatch.setattr(routing_mod, "_DISPATCH_TIMEOUT_S", 0.01)

    async def slow(qs: dict[str, str]) -> tuple[int, dict[str, Any]]:
        await asyncio.sleep(1.0)
        return 200, {}

    r = Router()
    r.add("GET", "/v1/slow", slow)
    app = RouterApp(r)
    status, _h, body = await _call(app, path="/v1/slow")
    assert status == 503
    payload = json.loads(body)
    assert payload["error"] == "timeout"
    assert "retry" in payload["hint"]


@pytest.mark.asyncio
async def test_app_decimal_body_serialized() -> None:
    def h(qs: dict[str, str]) -> tuple[int, dict[str, Any]]:
        return 200, {"price": Decimal("2.25")}

    r = Router()
    r.add("GET", "/v1/p", h)
    app = RouterApp(r)
    status, _h, body = await _call(app, path="/v1/p")
    assert status == 200
    assert json.loads(body) == {"price": 2.25}


@pytest.mark.asyncio
async def test_app_ignores_non_http_non_lifespan_scope() -> None:
    app = RouterApp(Router())
    sent: list[dict[str, Any]] = []

    async def receive() -> dict[str, Any]:  # pragma: no cover
        return {}

    async def send(msg: dict[str, Any]) -> None:
        sent.append(msg)

    await app({"type": "websocket"}, receive, send)
    assert sent == []


@pytest.mark.asyncio
async def test_app_lifespan_startup_and_shutdown() -> None:
    app = RouterApp(Router())
    messages = iter(
        [{"type": "lifespan.startup"}, {"type": "lifespan.shutdown"}]
    )
    sent: list[dict[str, Any]] = []

    async def receive() -> dict[str, Any]:
        return next(messages)

    async def send(msg: dict[str, Any]) -> None:
        sent.append(msg)

    await app({"type": "lifespan"}, receive, send)
    types = [m["type"] for m in sent]
    assert types == ["lifespan.startup.complete", "lifespan.shutdown.complete"]


# ---------------------------------------------------------------------------
# _EmbeddedServer signal-handler neutralization
# ---------------------------------------------------------------------------


def test_embedded_server_neutralizes_signals() -> None:
    import uvicorn

    config = uvicorn.Config(RouterApp(Router()), host="127.0.0.1", port=0)
    server = _EmbeddedServer(config)
    # install_signal_handlers is a no-op
    assert server.install_signal_handlers() is None
    # capture_signals is a context manager that simply yields
    with server.capture_signals() as cs:
        assert cs is None


@pytest.mark.asyncio
async def test_serve_embedded_starts_and_stops(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """serve_embedded returns (server, task) without binding a real port.

    We patch Server.serve to a no-op coroutine so no socket is opened.
    """
    import pytheum.routing as routing_mod

    started: dict[str, Any] = {}

    async def fake_serve(self: Any, sockets: Any = None) -> None:
        started["config"] = self.config
        # Return immediately — simulates a server that exits cleanly.

    monkeypatch.setattr(routing_mod._EmbeddedServer, "serve", fake_serve)

    server, task = serve_embedded(RouterApp(Router()), host="127.0.0.1", port=9)
    await task
    assert started["config"].host == "127.0.0.1"
    assert started["config"].port == 9
    assert started["config"].lifespan == "off"
    assert isinstance(task, asyncio.Task)
