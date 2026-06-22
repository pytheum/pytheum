"""The t_guide self-onboarding playbook + envelope/registry integration.

These also guard the whole MCP surface: that @enveloped didn't break FastMCP's
schema introspection, and that the guide never references a tool that isn't
actually served.
"""

from __future__ import annotations

import pytheum.mcp.server as server
from pytheum.mcp.guide import agent_guide, guide_tool_names


def test_guide_has_the_expected_shape() -> None:
    g = agent_guide()
    assert g["service"] == "pytheum"
    for key in ("summary", "principles", "conventions", "tool_groups", "workflows"):
        assert key in g, key
    assert g["conventions"]["read_only"] is True
    assert "response_envelope" in g["conventions"]
    assert len(g["workflows"]) >= 3
    assert any("equivalence" in p.lower() for p in g["principles"])


async def test_guide_references_only_registered_tools() -> None:
    """No drift: every tool the playbook names must actually be served."""
    registered = {t.name for t in await server.mcp.list_tools()}
    assert "t_guide" in registered
    missing = guide_tool_names() - registered
    assert not missing, f"guide references unregistered tools: {sorted(missing)}"


async def test_enveloped_did_not_break_schema_introspection() -> None:
    by_name = {t.name: t for t in await server.mcp.list_tools()}
    # the full surface is present (23 data tools + t_guide)
    assert len(by_name) >= 24, sorted(by_name)
    # a representative tool still exposes its real params (not *args/**kwargs)
    props = by_name["t_market_context"].inputSchema["properties"]
    assert "market_ref" in props
    assert "limit" in props
