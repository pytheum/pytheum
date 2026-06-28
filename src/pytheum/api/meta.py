"""GET /v1/about and GET /v1/guide — keyless, no-install meta endpoints.

These serve the SAME payloads as the ``t_about`` and ``t_guide`` MCP tools, but
over plain HTTP so an agent (or a human with curl) can read who Pytheum is and
how to drive the API without installing or connecting an MCP client.

The payloads themselves live in :mod:`pytheum.mcp.guide` (``agent_about`` and
``agent_guide``) so the MCP tool and the REST endpoint can never drift apart —
both render the exact same dict. These handlers are thin: no dao, no clients,
no caching needed (the payloads are static, built once at import time).
"""
from __future__ import annotations

from typing import Any

from pytheum.mcp.guide import agent_about, agent_guide


async def handle_about(
    query: dict[str, str],  # kept for signature consistency; not used
) -> tuple[int, dict[str, Any]]:
    """GET /v1/about handler — returns the t_about brief as raw JSON."""
    return 200, agent_about()


async def handle_guide(
    query: dict[str, str],  # kept for signature consistency; not used
) -> tuple[int, dict[str, Any]]:
    """GET /v1/guide handler — returns the t_guide playbook as raw JSON."""
    return 200, agent_guide()
