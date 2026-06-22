"""Coverage tests for pytheum.cli — arg parsing + command dispatch.

No server is started and no socket is bound: ``asyncio.run`` and ``_serve``
are mocked, and the captured coroutine is closed to avoid warnings.
"""
from __future__ import annotations

import json
from typing import Any

import pytest

import pytheum.cli as cli

# ---------------------------------------------------------------------------
# _print_banner
# ---------------------------------------------------------------------------


def test_print_banner_full(capsys: pytest.CaptureFixture[str]) -> None:
    cli._print_banner(
        "127.0.0.1",
        8080,
        eq_pairs=136123,
        rel_pairs=42,
        eq_version="2026-06-10",
        mcp_url="http://127.0.0.1:8444/mcp",
    )
    out = capsys.readouterr().out
    assert "pytheum" in out
    assert "http://127.0.0.1:8080" in out
    assert "136,123" in out  # thousands-formatted
    assert "42" in out
    assert "2026-06-10" in out
    assert "MCP:" in out
    assert "Ctrl-C" in out


def test_print_banner_version_lookup_failure(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def boom(_name: str) -> str:
        raise RuntimeError("no dist metadata")

    monkeypatch.setattr(cli.importlib.metadata, "version", boom)
    cli._print_banner(
        "127.0.0.1", 8080, eq_pairs=1, rel_pairs=1, eq_version=None, mcp_url=None
    )
    out = capsys.readouterr().out
    assert "pytheum unknown" in out  # falls back to "unknown"


def test_print_banner_without_mcp_or_version(
    capsys: pytest.CaptureFixture[str],
) -> None:
    cli._print_banner(
        "0.0.0.0",
        9000,
        eq_pairs=0,
        rel_pairs=0,
        eq_version=None,
        mcp_url=None,
    )
    out = capsys.readouterr().out
    assert "MCP:" not in out
    assert "dataset version" not in out
    assert "http://0.0.0.0:9000" in out


# ---------------------------------------------------------------------------
# main — argument parsing requires a subcommand
# ---------------------------------------------------------------------------


def test_main_no_command_exits_2() -> None:
    # required=True subparsers → argparse errors with exit code 2
    with pytest.raises(SystemExit) as exc:
        cli.main([])
    assert exc.value.code == 2


def test_main_unknown_command_exits_2() -> None:
    with pytest.raises(SystemExit) as exc:
        cli.main(["bogus"])
    assert exc.value.code == 2


def test_serve_help_exits_0() -> None:
    with pytest.raises(SystemExit) as exc:
        cli.main(["serve", "--help"])
    assert exc.value.code == 0


# ---------------------------------------------------------------------------
# main serve — dispatch wiring (mocked, no server)
# ---------------------------------------------------------------------------


def _patch_runner(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Patch _serve to a no-op coroutine and asyncio.run to capture its args."""
    captured: dict[str, Any] = {}

    async def fake_serve(host: str, port: int, *, mcp: bool) -> None:
        captured["host"] = host
        captured["port"] = port
        captured["mcp"] = mcp

    def fake_run(coro: Any) -> None:
        # Drive the coroutine to completion synchronously without an event loop.
        try:
            coro.send(None)
        except StopIteration:
            pass

    monkeypatch.setattr(cli, "_serve", fake_serve)
    monkeypatch.setattr(cli.asyncio, "run", fake_run)
    return captured


def test_serve_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    captured = _patch_runner(monkeypatch)
    cli.main(["serve"])
    assert captured == {"host": "127.0.0.1", "port": 8080, "mcp": False}


def test_serve_custom_host_port_and_mcp(monkeypatch: pytest.MonkeyPatch) -> None:
    captured = _patch_runner(monkeypatch)
    cli.main(["serve", "--host", "0.0.0.0", "--port", "9999", "--mcp"])
    assert captured == {"host": "0.0.0.0", "port": 9999, "mcp": True}


def test_serve_swallows_keyboard_interrupt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def raising_run(coro: Any) -> None:
        coro.close()  # avoid "never awaited"
        raise KeyboardInterrupt

    async def fake_serve(host: str, port: int, *, mcp: bool) -> None:  # pragma: no cover
        pass

    monkeypatch.setattr(cli, "_serve", fake_serve)
    monkeypatch.setattr(cli.asyncio, "run", raising_run)
    # Must not propagate KeyboardInterrupt
    cli.main(["serve"])


# ---------------------------------------------------------------------------
# main guide — prints JSON playbook
# ---------------------------------------------------------------------------


def test_guide_prints_json(capsys: pytest.CaptureFixture[str]) -> None:
    cli.main(["guide"])
    out = capsys.readouterr().out
    payload = json.loads(out)
    assert payload["service"] == "pytheum"
    assert "tool_groups" in payload


def test_guide_agent_flag_accepted(capsys: pytest.CaptureFixture[str]) -> None:
    cli.main(["guide", "--agent"])
    out = capsys.readouterr().out
    payload = json.loads(out)
    assert payload["service"] == "pytheum"


# ---------------------------------------------------------------------------
# _serve internals — exercise wiring with everything mocked (no socket)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_serve_wires_router_and_stops_on_signal(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """_serve builds the app, prints the banner, and shuts down cleanly.

    serve_embedded is replaced with a stub that returns a fake server + an
    already-finished task, so no port is bound. The stop_event is triggered
    immediately by stubbing loop.add_signal_handler to invoke the handler.
    """
    import pytheum.routing as routing_mod

    class _FakeIdx:
        pairs_loaded = 5
        dataset_version = "v-test"

    fake_server = type("S", (), {"should_exit": False})()

    async def _done_task() -> None:
        return None

    created: dict[str, Any] = {}

    def fake_serve_embedded(app: Any, *, host: str, port: int) -> Any:
        created["host"] = host
        created["port"] = port
        import asyncio as _a

        return fake_server, _a.ensure_future(_done_task())

    monkeypatch.setattr(routing_mod, "serve_embedded", fake_serve_embedded)
    # Indexes → avoid loading the real bundled datasets
    monkeypatch.setattr(
        "pytheum.equivalence.index.get_index", lambda: _FakeIdx()
    )
    monkeypatch.setattr("pytheum.related.index.get_index", lambda: _FakeIdx())

    # Trigger shutdown the instant a signal handler is registered.
    import asyncio as _asyncio

    loop = _asyncio.get_running_loop()
    orig_add = loop.add_signal_handler

    def insta_stop(sig: Any, handler: Any, *a: Any) -> None:
        handler()  # set the stop_event immediately

    monkeypatch.setattr(loop, "add_signal_handler", insta_stop)

    try:
        await cli._serve("127.0.0.1", 8080, mcp=False)
    finally:
        monkeypatch.setattr(loop, "add_signal_handler", orig_add)

    assert created == {"host": "127.0.0.1", "port": 8080}
    assert fake_server.should_exit is True
    out = capsys.readouterr().out
    assert "offline serve" in out


@pytest.mark.asyncio
async def test_serve_mcp_branch_starts_and_cancels(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """_serve with mcp=True spawns the MCP thread task and cancels it on exit."""
    import sys
    import types

    import pytheum.routing as routing_mod

    class _FakeIdx:
        pairs_loaded = 7
        dataset_version = None

    fake_server = type("S", (), {"should_exit": False})()

    async def _done_task() -> None:
        return None

    def fake_serve_embedded(app: Any, *, host: str, port: int) -> Any:
        import asyncio as _a

        return fake_server, _a.ensure_future(_done_task())

    monkeypatch.setattr(routing_mod, "serve_embedded", fake_serve_embedded)
    monkeypatch.setattr(
        "pytheum.equivalence.index.get_index", lambda: _FakeIdx()
    )
    monkeypatch.setattr("pytheum.related.index.get_index", lambda: _FakeIdx())

    # Stub the lazily-imported MCP server module so http_main blocks forever
    # (until the task is cancelled at shutdown) without binding a port.
    mcp_started = {"called": False}

    def fake_http_main() -> None:
        mcp_started["called"] = True
        # Return promptly. The thread task completes; _serve's shutdown still
        # exercises the mcp_task.cancel()/await path (cancelling an already
        # finished task is a no-op that is awaited cleanly).
        return

    fake_mod = types.ModuleType("pytheum.mcp.server")
    fake_mod.http_main = fake_http_main  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "pytheum.mcp.server", fake_mod)

    import asyncio as _asyncio

    loop = _asyncio.get_running_loop()
    orig_add = loop.add_signal_handler

    def insta_stop(sig: Any, handler: Any, *a: Any) -> None:
        handler()

    monkeypatch.setattr(loop, "add_signal_handler", insta_stop)

    try:
        await cli._serve("127.0.0.1", 8080, mcp=True)
    finally:
        monkeypatch.setattr(loop, "add_signal_handler", orig_add)

    out = capsys.readouterr().out
    assert "MCP:" in out  # banner shows the MCP url
    assert fake_server.should_exit is True


@pytest.mark.asyncio
async def test_serve_signal_handler_not_implemented(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """When add_signal_handler raises (Windows), _serve still shuts down.

    We make signal registration raise NotImplementedError and instead drive
    shutdown by patching the stop Event to be pre-set.
    """
    import asyncio as _asyncio

    import pytheum.routing as routing_mod

    class _FakeIdx:
        pairs_loaded = 1
        dataset_version = "x"

    fake_server = type("S", (), {"should_exit": False})()

    async def _done_task() -> None:
        return None

    def fake_serve_embedded(app: Any, *, host: str, port: int) -> Any:
        return fake_server, _asyncio.ensure_future(_done_task())

    monkeypatch.setattr(routing_mod, "serve_embedded", fake_serve_embedded)
    monkeypatch.setattr(
        "pytheum.equivalence.index.get_index", lambda: _FakeIdx()
    )
    monkeypatch.setattr("pytheum.related.index.get_index", lambda: _FakeIdx())

    loop = _asyncio.get_running_loop()

    def raise_not_impl(sig: Any, handler: Any, *a: Any) -> None:
        raise NotImplementedError

    monkeypatch.setattr(loop, "add_signal_handler", raise_not_impl)

    # Pre-set the Event so `await stop_event.wait()` returns immediately.
    class _SetEvent(_asyncio.Event):
        def __init__(self) -> None:
            super().__init__()
            self.set()

    monkeypatch.setattr(cli.asyncio, "Event", _SetEvent)

    await cli._serve("127.0.0.1", 8080, mcp=False)
    assert fake_server.should_exit is True
