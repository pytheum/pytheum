"""Tests for GET /v1/markets/{ref}/mm_reference.

Handler tests call handle_market_mm_reference directly with a fake DAO and a fake
EquivalenceIndex (no real disk I/O), reusing the /rules test fixtures — the handler
composes the real /equivalents + /rules handlers, so this is a true end-to-end check.
"""
from __future__ import annotations

import pytest

from pytheum.api.markets_mm import handle_market_mm_reference
from tests.test_markets_rules import _KALSHI_ROW, _PM_ROW, _make_index, _SimpleDao


@pytest.mark.asyncio
async def test_mm_reference_full_record():
    dao = _SimpleDao({"kalshi:KX-TEST-YES": _KALSHI_ROW, "polymarket:12345": _PM_ROW})
    status, body = await handle_market_mm_reference(
        "kalshi:KX-TEST-YES", {}, dao=dao, equivalence=_make_index())
    assert status == 200
    mm = body["mm_reference"]
    # kalshi mid 0.65 (0.63/0.67), pm implied 0.62 -> p_hat between, basis ~ +0.03
    assert 0.60 <= mm["p_hat"] <= 0.66
    assert mm["basis"] is not None and abs(mm["basis"] - 0.03) < 0.02
    assert mm["risk_inputs"]["terminal_variance"] is not None
    assert mm["risk_inputs"]["time_to_resolution_years"] is not None
    # confidence 1.0 clears the fungibility floor even for a non-deterministic method
    assert mm["fungibility"]["fungible"] is True
    assert body["pair"]["orientation"] == "known"
    assert body["pair"]["kalshi"]["ref"] == "kalshi:KX-TEST-YES"
    assert body["pair"]["polymarket"]["ref"] == "polymarket:12345"
    assert body["meta"]["pairs_loaded"] == 1
    assert body["illustrative_quote"] is not None


@pytest.mark.asyncio
async def test_mm_reference_pm_focal():
    dao = _SimpleDao({"kalshi:KX-TEST-YES": _KALSHI_ROW, "polymarket:12345": _PM_ROW})
    status, body = await handle_market_mm_reference(
        "polymarket:12345", {}, dao=dao, equivalence=_make_index())
    assert status == 200
    assert body["pair"]["kalshi"]["ref"] == "kalshi:KX-TEST-YES"
    assert body["pair"]["polymarket"]["ref"] == "polymarket:12345"
    assert body["mm_reference"]["p_hat"] is not None


@pytest.mark.asyncio
async def test_mm_reference_focal_only_single_leg():
    """Counterpart absent from the store -> single-leg reference, flagged one_leg_missing."""
    dao = _SimpleDao({"kalshi:KX-TEST-YES": _KALSHI_ROW})  # no PM row
    status, body = await handle_market_mm_reference(
        "kalshi:KX-TEST-YES", {}, dao=dao, equivalence=_make_index())
    assert status == 200
    assert body["mm_reference"]["p_hat"] is not None       # kalshi leg alone still yields a reference
    assert any("one_leg_missing" in w for w in body["mm_reference"]["warnings"])


@pytest.mark.asyncio
async def test_mm_reference_unknown_market_degrades():
    """Unknown ref (not in store, not in index) degrades to a single-leg-missing record, not a crash."""
    dao = _SimpleDao({})
    status, body = await handle_market_mm_reference(
        "kalshi:UNKNOWN", {}, dao=dao, equivalence=_make_index(file_missing=True))
    assert status == 200
    assert "mm_reference" in body
    assert any("one_leg_missing" in w for w in body["mm_reference"]["warnings"])
