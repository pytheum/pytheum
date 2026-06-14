"""pytheum CLI — standalone offline serve + MCP entrypoint.

Usage
-----
    pytheum serve [--host HOST] [--port PORT] [--mcp]

``pytheum serve`` starts an offline-capable HTTP API that serves the bundled
equivalence/related/matched/rules/status datasets without any database,
secrets, or external API keys.

Routes that are live offline (bundled datasets)
------------------------------------------------
    GET /v1/status                      dataset summary + service version
    GET /v1/markets/equivalents         collection of 136k+ verified pairs
    GET /v1/markets/matched             paginated pair browser
    GET /v1/markets/{ref}/equivalents   per-market lookup
    GET /v1/markets/{ref}/rules         resolution rules for both legs
    GET /v1/markets/{ref}/related       correlated (non-equivalent) pairs
    GET /llms.txt                       MCP tool inventory (plain text)
    GET /healthz                        liveness probe

Routes that degrade gracefully (require live venue API keys)
------------------------------------------------------------
    GET /v1/markets/screen              → 200 {degraded: true, markets: []}
    GET /v1/markets/{ref}/book          → 200 {degraded: true}
    GET /v1/markets/{ref}/trades        → 200 {degraded: true}
    GET /v1/markets/{ref}/oi            → 200 {degraded: true}
    GET /v1/markets/{ref}/ohlcv         → 200 {degraded: true}
    GET /v1/markets/{ref}/holders       → 200 {degraded: true}
    GET /v1/markets/whale-trades        → 200 {degraded: true}
    GET /v1/traders/leaderboard         → 200 {degraded: true}
    GET /v1/traders/{wallet}            → 200 {degraded: true}
"""
from __future__ import annotations

import argparse
import asyncio
import importlib.metadata
import logging
import signal
import sys

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Banner
# ---------------------------------------------------------------------------


def _print_banner(
    host: str,
    port: int,
    *,
    eq_pairs: int,
    rel_pairs: int,
    eq_version: str | None,
    mcp_url: str | None,
) -> None:
    ver = "unknown"
    try:
        ver = importlib.metadata.version("pytheum")
    except Exception:
        pass

    bar = "=" * 60
    print(bar)
    print(f"  pytheum {ver} — offline serve")
    print(bar)
    print(f"  HTTP API:  http://{host}:{port}")
    if mcp_url:
        print(f"  MCP:       {mcp_url}  (streamable-HTTP)")
    print()
    print("  Bundled datasets:")
    print(f"    equivalence pairs : {eq_pairs:,}")
    print(f"    related pairs     : {rel_pairs:,}")
    if eq_version:
        print(f"    dataset version   : {eq_version}")
    print()
    print("  Live routes (bundled data):")
    for r in [
        "GET /v1/status",
        "GET /v1/markets/equivalents",
        "GET /v1/markets/matched",
        "GET /v1/markets/{ref}/equivalents",
        "GET /v1/markets/{ref}/rules",
        "GET /v1/markets/{ref}/related",
        "GET /llms.txt",
        "GET /healthz",
    ]:
        print(f"    {r}")
    print()
    print("  Degraded (no secrets): /screen  /book  /trades  /oi  /ohlcv")
    print("                          /holders  /whale-trades  /traders/*")
    print()
    print("  Press Ctrl-C to stop.")
    print(bar)


# ---------------------------------------------------------------------------
# Serve command
# ---------------------------------------------------------------------------


async def _serve(host: str, port: int, *, mcp: bool) -> None:
    from pytheum.api import register_all
    from pytheum.equivalence.index import get_index as get_eq_index
    from pytheum.registry import RouterRegistry
    from pytheum.related.index import get_index as get_rel_index
    from pytheum.routing import RouterApp, serve_embedded

    # Eagerly load indexes so pair counts are available for the banner.
    eq_idx = get_eq_index()
    rel_idx = get_rel_index()

    registry = RouterRegistry()
    register_all(registry, dao=None, equivalence=eq_idx, related=rel_idx, clients=None)
    router = registry.build_router()
    app = RouterApp(router)

    mcp_url: str | None = None
    mcp_task: asyncio.Task[None] | None = None

    if mcp:
        # Import lazily so startup without --mcp has no MCP overhead.
        import os

        os.environ.setdefault("PYTHEUM_API_BASE", f"http://{host}:{port}")
        from pytheum.mcp.server import http_main

        mcp_port = 8444
        mcp_url = f"http://{host}:{mcp_port}/mcp"
        # http_main is synchronous (calls uvicorn.run) — run it in a thread
        mcp_task = asyncio.create_task(asyncio.to_thread(http_main))

    _print_banner(
        host,
        port,
        eq_pairs=eq_idx.pairs_loaded,
        rel_pairs=rel_idx.pairs_loaded,
        eq_version=eq_idx.dataset_version,
        mcp_url=mcp_url,
    )

    server, task = serve_embedded(app, host=host, port=port)

    loop = asyncio.get_running_loop()

    stop_event = asyncio.Event()

    def _handle_sigint() -> None:
        print("\nShutting down…")
        stop_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _handle_sigint)
        except (NotImplementedError, RuntimeError):
            # Windows / environments where signal handlers can't be set.
            pass

    await stop_event.wait()
    server.should_exit = True
    await task
    if mcp_task is not None:
        mcp_task.cancel()
        try:
            await mcp_task
        except asyncio.CancelledError:
            pass


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> None:
    """Entry point for the ``pytheum`` console script."""
    parser = argparse.ArgumentParser(
        prog="pytheum",
        description="Pytheum — verified prediction-market graph CLI.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    serve_p = sub.add_parser(
        "serve",
        help="Start the offline HTTP API (no secrets required).",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    serve_p.add_argument(
        "--host",
        default="127.0.0.1",
        help="Bind host (default: 127.0.0.1).",
    )
    serve_p.add_argument(
        "--port",
        type=int,
        default=8080,
        help="Bind port (default: 8080).",
    )
    serve_p.add_argument(
        "--mcp",
        action="store_true",
        help=(
            "Also start the MCP server on port 8444 "
            "(streamable-HTTP, auto-pointed at the local API)."
        ),
    )

    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.WARNING,
        format="%(levelname)s %(name)s %(message)s",
    )

    if args.command == "serve":
        try:
            asyncio.run(_serve(args.host, args.port, mcp=args.mcp))
        except KeyboardInterrupt:
            pass
    else:
        parser.print_help()
        sys.exit(1)
