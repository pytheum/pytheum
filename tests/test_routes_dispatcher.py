"""Unit tests for pytheum.routing.Router — adapted from pytheum-stream."""
from __future__ import annotations

import pytest

from pytheum.routing import Router


def _h_market(ref: str, qs: dict[str, str]) -> dict[str, object]:
    return {"matched": "market", "ref": ref, "qs": qs}


def _h_bundle(ref: str, qs: dict[str, str]) -> dict[str, object]:
    return {"matched": "bundle", "ref": ref, "qs": qs}


@pytest.mark.asyncio
async def test_router_matches_market_context_path() -> None:
    r = Router()
    r.add("GET", "/v1/markets/{ref}/context", _h_market)
    out = await r.dispatch("GET", "/v1/markets/polymarket%3A0xabc/context", {"limit": "30"})
    assert out["matched"] == "market"
    assert out["ref"] == "polymarket:0xabc"  # URL-decoded
    assert out["qs"] == {"limit": "30"}


@pytest.mark.asyncio
async def test_router_matches_bundle_path() -> None:
    r = Router()
    r.add("GET", "/v1/bundles/{ref}/context", _h_bundle)
    out = await r.dispatch("GET", "/v1/bundles/kalshi:FED-25/context", {})
    assert out["matched"] == "bundle"
    assert out["ref"] == "kalshi:FED-25"


@pytest.mark.asyncio
async def test_router_returns_none_on_no_match() -> None:
    r = Router()
    r.add("GET", "/v1/markets/{ref}/context", _h_market)
    out = await r.dispatch("GET", "/some/other/path", {})
    assert out is None


@pytest.mark.asyncio
async def test_router_method_mismatch_returns_none() -> None:
    r = Router()
    r.add("GET", "/v1/status", _h_market)
    out = await r.dispatch("POST", "/v1/status", {})
    assert out is None


@pytest.mark.asyncio
async def test_router_trailing_slash_matches() -> None:
    def _h_status(qs: dict[str, str]) -> dict[str, object]:
        return {"ok": True}

    r = Router()
    r.add("GET", "/v1/status", _h_status)
    out = await r.dispatch("GET", "/v1/status/", {})
    # trailing slash is accepted by the regex (/?$)
    assert out is not None
