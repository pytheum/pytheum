"""Uniform MCP tool response envelope: ``{ok, command, data, meta}``.

Every MCP tool returns one shape so an agent can branch on ``ok``, read
provenance from ``meta``, and parse the payload from ``data`` without per-tool
special-casing:

    success: {"ok": true,  "command": "t_x", "data": <payload>, "meta": {...}}
    error:   {"ok": false, "command": "t_x", "error": "...",
              "data": <payload|null>, "meta": {...}}

``data`` carries the tool's original payload verbatim (lossless — existing
fields are unchanged, just nested one level under ``data``), so the migration is
mechanical for consumers. An error is signalled either by the tool returning an
``{"error": ...}`` sentinel (bad input) or by an exception (caught here and
surfaced structured, never as a raw transport 500). A venue being down
(``{"source": "unavailable"}``) is a SUCCESSFUL degraded response, not an error.

``meta`` carries ``generated_at`` (UTC ISO-8601), ``elapsed_ms``, and the
package ``version`` for provenance/debugging.

Kill switch: set ``PYTHEUM_MCP_ENVELOPE=0`` to return raw tool payloads
(pre-envelope behaviour) with no redeploy — a safety valve for the live remote
connector if a client can't yet handle the enveloped shape.
"""
from __future__ import annotations

import functools
import os
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

try:  # package metadata is absent in some editable/source runs
    from importlib.metadata import PackageNotFoundError
    from importlib.metadata import version as _pkg_version

    try:
        _VERSION = _pkg_version("pytheum")
    except PackageNotFoundError:
        _VERSION = "0.0.0"
except Exception:  # pragma: no cover - importlib.metadata always present on 3.11+
    _VERSION = "0.0.0"


def _envelope_enabled() -> bool:
    """Read the kill switch at call time so it can be toggled without reimport."""
    return os.environ.get("PYTHEUM_MCP_ENVELOPE", "1") != "0"


def _meta(elapsed_ms: float) -> dict[str, Any]:
    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "elapsed_ms": round(elapsed_ms, 1),
        "version": _VERSION,
    }


def ok(command: str, data: Any, *, elapsed_ms: float = 0.0) -> dict[str, Any]:
    """Build a success envelope."""
    return {"ok": True, "command": command, "data": data, "meta": _meta(elapsed_ms)}


def err(command: str, error: str, *, data: Any = None,
        elapsed_ms: float = 0.0) -> dict[str, Any]:
    """Build an error envelope (``data`` preserves the original payload, if any)."""
    return {"ok": False, "command": command, "error": error, "data": data,
            "meta": _meta(elapsed_ms)}


def _error_sentinel(result: Any) -> str | None:
    """Return the error message if ``result`` is a tool error sentinel, else None.

    Tools signal bad input as ``{"error": "...", "hint": "..."}``. A venue outage
    (``{"source": "unavailable"}``) is intentionally NOT treated as an error — it
    is a valid degraded response the agent should still see under ``ok: true``.
    """
    if isinstance(result, dict) and result.get("error"):
        e = result["error"]
        return e if isinstance(e, str) else str(e)
    return None


def enveloped(fn: Callable[..., Awaitable[Any]]) -> Callable[..., Awaitable[Any]]:
    """Wrap an async MCP tool so its return is the ``{ok, command, data, meta}`` envelope.

    ``functools.wraps`` preserves ``__name__``/``__doc__``/``__annotations__`` and
    sets ``__wrapped__``, so FastMCP's signature + schema introspection is
    unchanged (it unwraps to the original function). ``command`` is the tool's
    function name. Honours the ``PYTHEUM_MCP_ENVELOPE`` kill switch.
    """
    command = fn.__name__

    @functools.wraps(fn)
    async def wrapper(*args: Any, **kwargs: Any) -> Any:
        if not _envelope_enabled():
            return await fn(*args, **kwargs)
        start = time.monotonic()
        try:
            result = await fn(*args, **kwargs)
        except Exception as exc:  # never leak a raw exception to the transport
            return err(command, f"{type(exc).__name__}: {exc}",
                       elapsed_ms=(time.monotonic() - start) * 1000.0)
        elapsed_ms = (time.monotonic() - start) * 1000.0
        sentinel = _error_sentinel(result)
        if sentinel is not None:
            return err(command, sentinel, data=result, elapsed_ms=elapsed_ms)
        return ok(command, result, elapsed_ms=elapsed_ms)

    return wrapper
