"""Unit tests for pytheum.mm.compose.assemble_mm_reference (pure fusion of the
/equivalents + /rules payloads into the MM reference record)."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

from pytheum.mm import assemble_mm_reference

_FUTURE = (datetime.now(UTC) + timedelta(days=30)).isoformat()


def _equ(*, kalshi_implied=0.65, pm_implied=0.62, spread=0.03, spread_unavailable=None,
         focal_venue="kalshi", k_book=None, pm_book=None, counterpart_id="polymarket:12345"):
    """An /equivalents-shaped payload (kalshi focal by default)."""
    cross = {}
    if kalshi_implied is not None:
        cross["kalshi_implied"] = kalshi_implied
    if pm_implied is not None:
        cross["pm_implied"] = pm_implied
    if spread is not None:
        cross["spread"] = spread
    if spread_unavailable is not None:
        cross["spread"] = None
        cross["spread_unavailable"] = spread_unavailable
    focal_book = k_book if focal_venue == "kalshi" else pm_book
    cp_book = pm_book if focal_venue == "kalshi" else k_book
    cp_implied = pm_implied if focal_venue == "kalshi" else kalshi_implied
    equivalents = []
    if counterpart_id is not None:
        equivalents = [{"id": counterpart_id, "venue": "polymarket" if focal_venue == "kalshi"
                        else "kalshi", "implied_yes": cp_implied, "book": cp_book}]
    return {
        "market": {"id": "kalshi:KX-T" if focal_venue == "kalshi" else "polymarket:12345",
                   "venue": focal_venue, "book": focal_book},
        "equivalents": equivalents,
        "cross_venue": cross,
        "meta": {"pairs_loaded": 1, "dataset_version": "v1", "matched_via": "kalshi_ticker"},
    }


def _rul(*, method="structured_key", confidence=1.0, same_day=True,
         k_rules="Resolves to the official final result.",
         pm_rules="Resolves based on the official final result."):
    return {
        "market": {"venue": "kalshi", "resolution": k_rules, "resolution_at": _FUTURE},
        "equivalent": {"venue": "polymarket", "resolution": pm_rules, "resolution_at": _FUTURE},
        "comparison": {"method": method, "confidence": confidence, "same_deadline_day": same_day},
    }


def test_oriented_fungible_pair_full_record():
    out = assemble_mm_reference("kalshi:KX-T", _equ(
        k_book={"spread": 0.02, "bid_size": 500, "ask_size": 500},
        pm_book={"spread": 0.01, "bid_size": 800, "ask_size": 800}), _rul())
    mm = out["mm_reference"]
    assert mm["p_hat"] is not None
    # tighter+deeper PM leg pulls p_hat toward 0.62
    assert 0.62 <= mm["p_hat"] <= 0.65
    assert abs(mm["basis"] - 0.03) < 1e-6
    assert mm["fungibility"]["fungible"] is True
    assert mm["warnings"] == []
    assert mm["risk_inputs"]["terminal_variance"] is not None
    assert out["pair"]["orientation"] == "known"
    assert out["illustrative_quote"] is not None            # fungible + p_hat + T -> illustrative present
    assert 0.0 <= out["illustrative_quote"]["bid"] <= out["illustrative_quote"]["ask"] <= 1.0


def test_unoriented_pair_drops_pm_leg():
    out = assemble_mm_reference("kalshi:KX-T", _equ(spread_unavailable="unoriented"), _rul())
    assert out["pair"]["orientation"] == "unknown"
    assert out["pair"]["polymarket"]["implied_yes"] is None   # PM leg dropped from the reference
    assert any("orientation_unknown" in w for w in out["mm_reference"]["warnings"])
    assert any("one_leg_missing" in w for w in out["mm_reference"]["warnings"])


def test_single_leg_no_counterpart():
    out = assemble_mm_reference("kalshi:KX-T", _equ(pm_implied=None, spread=None,
                                                    counterpart_id=None), _rul())
    assert out["mm_reference"]["p_hat"] is not None           # single live leg still yields a reference
    assert any("one_leg_missing" in w for w in out["mm_reference"]["warnings"])


def test_divergent_rules_veto_fungibility():
    out = assemble_mm_reference("kalshi:KX-T", _equ(), _rul(
        method="structured_key", confidence=1.0,
        k_rules="Resolves YES if CPI is above 3.0% per BLS.",
        pm_rules="Resolves YES if CPI exceeds 3.25%."))
    assert out["mm_reference"]["fungibility"]["fungible"] is False   # rules veto a confident match
    assert out["illustrative_quote"] is None                        # not fungible -> no illustrative quote
    assert any("settlement_divergence" in w for w in out["mm_reference"]["warnings"])


def test_deadline_mismatch_warns():
    out = assemble_mm_reference("kalshi:KX-T", _equ(), _rul(same_day=False))
    assert any("deadline_mismatch" in w for w in out["mm_reference"]["warnings"])


def test_pm_focal_orientation_maps_legs():
    """Focal on the PM side: the kalshi leg comes from the counterpart, refs map correctly."""
    out = assemble_mm_reference("polymarket:12345", _equ(
        focal_venue="polymarket", counterpart_id="kalshi:KX-T"), _rul())
    assert out["pair"]["kalshi"]["ref"] == "kalshi:KX-T"
    assert out["pair"]["polymarket"]["ref"] == "polymarket:12345"
    assert out["pair"]["kalshi"]["implied_yes"] == 0.65
    assert out["pair"]["polymarket"]["implied_yes"] == 0.62
