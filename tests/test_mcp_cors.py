"""CORS on the streamable-http MCP app.

Regression for the "Checking connection…" hang: browser-based MCP clients
(claude.ai / claude.com web custom connectors) send a CORS preflight and must
read the `mcp-session-id` response header. Without CORS the preflight 405s and
the connector never completes — even though the same endpoint works in curl
(which ignores CORS). These tests assert the preflight is answered with the
right headers and that the session header is exposed.
"""

from __future__ import annotations

import httpx

from pytheum.mcp.server import _CORS_EXPOSE_HEADERS, _build_http_app


async def test_mcp_cors_preflight_allows_browser_connectors() -> None:
    app = _build_http_app()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        r = await c.options(
            "/mcp",
            headers={
                "Origin": "https://claude.ai",
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "content-type,mcp-session-id",
            },
        )
    # Preflight must succeed (was 405 before the fix → connector hung).
    assert r.status_code in (200, 204), r.status_code
    origin = r.headers.get("access-control-allow-origin")
    assert origin in ("*", "https://claude.ai"), origin
    assert "POST" in (r.headers.get("access-control-allow-methods") or "").upper()


def test_mcp_exposes_session_header() -> None:
    # Browser clients can only read mcp-session-id if it is exposed; the actual
    # response header is added by CORSMiddleware on non-preflight responses, so
    # assert the config that drives it (the live response is verified on deploy).
    assert "mcp-session-id" in _CORS_EXPOSE_HEADERS
