"""URL dispatcher and ASGI adapter for the Pytheum serve layer.

Contains two public components:

Router
    Tiny URL dispatcher.  Patterns use ``{name}`` segments.  Home-grown
    because pytheum-stream is a websockets server, not a FastAPI app; this
    gives pattern-matching without dragging in Starlette/FastAPI.

RouterApp
    Minimal ASGI callable dispatching GETs to a :class:`Router` (#244).
    The websockets ``process_request`` shim serves exactly one HTTP response
    per TCP connection — no keep-alive — and slow handlers race the WS
    handshake ``open_timeout``.  Under concurrent load Caddy reuses pooled
    upstream connections and hits closing sockets, so parallel agent calls
    502 (measured 2026-06-11: 12/12 parallel /context requests → 502).
    This module exposes the same Router through an embedded uvicorn server
    on a separate port.

Also exports :func:`serve_embedded` for starting uvicorn inside an existing
event loop without stealing process signal handlers.

Copied from pytheum-stream api/routes.py + api/asgi.py at Stage 3a of the
2026-06-13 restructure.  Stage 3b will delete the originals from stream.
"""
from __future__ import annotations

import asyncio
import contextlib
import inspect
import json
import logging
import re
from collections.abc import Awaitable, Callable, Iterator
from decimal import Decimal
from typing import Any
from urllib.parse import parse_qs, unquote

import uvicorn

from pytheum.llms_txt import LLMS_TXT

logger = logging.getLogger(__name__)

Handler = Callable[..., Awaitable[Any]]

# Hard ceiling on handler time.  The MCP transport drops calls that exceed
# its patience (~15-20 s observed) with an OPAQUE "Server disconnected" —
# the worst failure mode the 2026-06-11 benchmark found.  Better to answer
# 503 with a retry hint than to let the connection die silently.
_DISPATCH_TIMEOUT_S = 14.0


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------


class Router:
    """Pattern-matching URL dispatcher.  Patterns use ``{name}`` segments."""

    def __init__(self) -> None:
        self._routes: list[tuple[str, re.Pattern[str], list[str], Handler]] = []

    def add(self, method: str, pattern: str, handler: Handler) -> None:
        names: list[str] = []
        regex_parts: list[str] = []
        for seg in pattern.strip("/").split("/"):
            if seg.startswith("{") and seg.endswith("}"):
                name = seg[1:-1]
                names.append(name)
                regex_parts.append(r"([^/]+)")
            else:
                regex_parts.append(re.escape(seg))
        compiled = re.compile("^/" + "/".join(regex_parts) + "/?$")
        self._routes.append((method.upper(), compiled, names, handler))

    async def dispatch(
        self, method: str, path: str, query: dict[str, str]
    ) -> Any | None:
        method = method.upper()
        for m, pat, _names, handler in self._routes:
            if m != method:
                continue
            mo = pat.match(path)
            if not mo:
                continue
            args = [unquote(v) for v in mo.groups()]
            result = handler(*args, query)
            if inspect.isawaitable(result):
                return await result
            return result
        return None


# ---------------------------------------------------------------------------
# ASGI adapter
# ---------------------------------------------------------------------------


class DecimalEncoder(json.JSONEncoder):
    """JSON encoder that converts Decimal to float (Postgres NUMERIC columns)."""

    def default(self, o: object) -> Any:
        if isinstance(o, Decimal):
            return float(o)
        return super().default(o)


class RouterApp:
    """Minimal ASGI callable dispatching GETs to a :class:`Router`.

    Deliberately tiny: the Router already owns path matching and the
    handlers already return ``(status, body)`` tuples, so a framework
    (Starlette/FastAPI) would only add a second routing layer.
    """

    def __init__(
        self,
        router: Router,
        *,
        llms_txt: str | None = None,
        text_routes: dict[str, Callable[[], str]] | None = None,
    ) -> None:
        self._router = router
        # Wrapper processes (pytheum-pit) extend the agent manifest with
        # sections for routes only they serve (e.g. the cross_venue_arb WS
        # event) — without an override the manifest they serve on :8445 would
        # silently omit them (the 2026-07-03 launch-night artifact).
        self._llms_txt = llms_txt if llms_txt is not None else LLMS_TXT
        # Plain-text GET endpoints (e.g. Prometheus /v1/stream/metrics) that
        # can't go through the JSON router. Callable is invoked per request.
        self._text_routes = dict(text_routes or {})

    async def __call__(
        self, scope: dict[str, Any], receive: Any, send: Any
    ) -> None:
        if scope["type"] == "lifespan":
            await self._lifespan(receive, send)
            return
        if scope["type"] != "http":
            return
        path: str = scope["path"]
        if path == "/healthz":
            await self._respond(send, 200, {"ok": True})
            return
        if path.rstrip("/") in ("/llms.txt",):
            await self._respond_text(send, self._llms_txt)
            return
        text_handler = self._text_routes.get(path.rstrip("/") or path)
        if text_handler is not None:
            try:
                body = text_handler()
            except Exception:
                await self._respond(send, 500, {"error": "text_route_failed"})
                return
            await self._respond_text(send, body)
            return
        raw_qs = parse_qs(
            scope.get("query_string", b"").decode("utf-8", "replace")
        )
        query = {k: v[0] for k, v in raw_qs.items()}
        try:
            result = await asyncio.wait_for(
                self._router.dispatch(scope["method"], path, query),
                timeout=_DISPATCH_TIMEOUT_S,
            )
        except TimeoutError:
            logger.warning(
                "dispatch timeout (%ss) on %s %s",
                _DISPATCH_TIMEOUT_S,
                scope["method"],
                path,
            )
            await self._respond(
                send,
                503,
                {
                    "error": "timeout",
                    "hint": (
                        f"the server could not answer within "
                        f"{_DISPATCH_TIMEOUT_S:.0f}s "
                        "(cold cache or heavy load) — retry in a few seconds, "
                        "or narrow the request (smaller limit / tighter filters)."
                    ),
                },
            )
            return
        except Exception:
            logger.exception(
                "unhandled error on %s %s", scope["method"], path
            )
            await self._respond(send, 500, {"error": "internal_error"})
            return
        if result is None:
            await self._respond(send, 404, {"detail": "not found"})
            return
        status, body = result
        await self._respond(send, status, body)

    @staticmethod
    async def _respond_text(send: Any, body: str, status: int = 200) -> None:
        await send(
            {
                "type": "http.response.start",
                "status": status,
                "headers": [(b"content-type", b"text/plain; charset=utf-8")],
            }
        )
        await send({"type": "http.response.body", "body": body.encode()})

    @staticmethod
    async def _respond(send: Any, status: int, body: Any) -> None:
        payload = json.dumps(body, cls=DecimalEncoder).encode()
        await send(
            {
                "type": "http.response.start",
                "status": status,
                "headers": [(b"content-type", b"application/json")],
            }
        )
        await send({"type": "http.response.body", "body": payload})

    @staticmethod
    async def _lifespan(receive: Any, send: Any) -> None:
        while True:
            message = await receive()
            if message["type"] == "lifespan.startup":
                await send({"type": "lifespan.startup.complete"})
            elif message["type"] == "lifespan.shutdown":
                await send({"type": "lifespan.shutdown.complete"})
                return


# ---------------------------------------------------------------------------
# Embedded uvicorn server
# ---------------------------------------------------------------------------


class _EmbeddedServer(uvicorn.Server):
    """uvicorn.Server that never touches process signal handlers.

    server_run installs ``loop.add_signal_handler`` for graceful shutdown
    (which saves the rolling-index snapshot); uvicorn's signal capture
    would steal SIGTERM from it.  Both the pre-0.29 and current hook names
    are neutralized.
    """

    def install_signal_handlers(self) -> None:  # uvicorn < 0.29
        return

    @contextlib.contextmanager
    def capture_signals(self) -> Iterator[None]:  # uvicorn >= 0.29
        yield


def serve_embedded(
    app: Any, *, host: str, port: int
) -> tuple[uvicorn.Server, asyncio.Task[None]]:
    """Start uvicorn inside the current event loop; return ``(server, task)``.

    Graceful shutdown: set ``server.should_exit = True`` and await the task.
    """
    config = uvicorn.Config(
        app,
        host=host,
        port=port,
        log_level="warning",
        access_log=False,
        lifespan="off",
    )
    server = _EmbeddedServer(config)
    task = asyncio.create_task(server.serve())
    return server, task
