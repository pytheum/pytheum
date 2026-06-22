"""The {ok, command, data, meta} MCP response envelope (pytheum.mcp.envelope)."""

from __future__ import annotations

import inspect

from pytheum.mcp.envelope import enveloped


async def test_success_envelope_wraps_payload_under_data() -> None:
    @enveloped
    async def t_demo(x: int = 1) -> dict:
        return {"value": x}

    out = await t_demo(5)
    assert out["ok"] is True
    assert out["command"] == "t_demo"
    assert out["data"] == {"value": 5}  # original payload, lossless, under data
    assert set(out["meta"]) == {"generated_at", "elapsed_ms", "version"}


async def test_error_sentinel_becomes_ok_false_losslessly() -> None:
    @enveloped
    async def t_bad() -> dict:
        return {"error": "bad ref", "hint": "use venue:prefix"}

    out = await t_bad()
    assert out["ok"] is False
    assert out["command"] == "t_bad"
    assert out["error"] == "bad ref"
    # the full original (incl. hint) is preserved under data
    assert out["data"] == {"error": "bad ref", "hint": "use venue:prefix"}


async def test_exception_is_caught_as_error_envelope() -> None:
    @enveloped
    async def t_boom() -> dict:
        raise ValueError("nope")

    out = await t_boom()
    assert out["ok"] is False
    assert "ValueError: nope" in out["error"]
    assert out["data"] is None


async def test_venue_unavailable_is_a_degraded_success_not_an_error() -> None:
    @enveloped
    async def t_venue() -> dict:
        return {"source": "unavailable", "venue": "kalshi"}

    out = await t_venue()
    assert out["ok"] is True  # venue outage is a valid degraded response
    assert out["data"]["source"] == "unavailable"


async def test_kill_switch_returns_raw_payload(monkeypatch) -> None:
    monkeypatch.setenv("PYTHEUM_MCP_ENVELOPE", "0")

    @enveloped
    async def t_raw() -> dict:
        return {"value": 1}

    out = await t_raw()
    assert out == {"value": 1}  # no envelope when disabled


def test_wraps_preserves_name_and_signature_for_fastmcp() -> None:
    @enveloped
    async def t_sig(market_ref: str, limit: int = 25) -> dict:
        return {}

    assert t_sig.__name__ == "t_sig"
    # FastMCP introspects the signature to build the tool schema — it must see
    # the original params, not (*args, **kwargs).
    assert list(inspect.signature(t_sig).parameters) == ["market_ref", "limit"]
