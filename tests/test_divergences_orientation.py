"""find_divergences orientation gate.

Regression for the "scanner is dark" eval finding: house_party (binary
party-win) pairs were dropped as `orientation_excluded` because only `event`
was treated as self-oriented. They quote the SAME outcome on both venues
(Kalshi ticker -D/-R matches the PM party), so direct orientation is correct
and they must be scanned. Game/match types still require the side-map.
"""

from __future__ import annotations

import pytheum.mcp.tools as tools
from pytheum.mcp.tools import _SELF_ORIENTED_BET_TYPES, find_divergences


def test_self_oriented_set_includes_binary_excludes_game_types() -> None:
    # Binary single-outcome (YES==YES, no side-map needed).
    assert "event" in _SELF_ORIENTED_BET_TYPES
    assert "house_party" in _SELF_ORIENTED_BET_TYPES
    # Game/match types MUST stay out — they need the verified side-map, or we
    # reintroduce the team-orientation false edges the side-map was built for.
    for bt in ("moneyline", "spread", "total", "tennis_ml", "ufc_ml", "esports_map"):
        assert bt not in _SELF_ORIENTED_BET_TYPES, bt


def _house_party_pair() -> dict:
    # Synthetic but shaped like /v1/markets/equivalents pairs: both legs booked,
    # poly_side absent (no side-map), bet_type house_party.
    return {
        "bet_type": "house_party",
        "poly_side": None,
        "method": "opus_backstop",
        "confidence": 1.0,
        "a": {"venue": "kalshi", "question": "Will the Democratic Party win KS-03?",
              "days_to_resolution": 100,
              "book": {"bid": 0.55, "ask": 0.57}},
        "b": {"venue": "polymarket", "question": "Will the Democratic Party win the KS-03?",
              "days_to_resolution": 100,
              "book": {"bid": 0.54, "ask": 0.56}},
    }


async def test_house_party_pair_not_orientation_excluded(monkeypatch) -> None:
    async def _fake_get(path, params, base_url):  # noqa: ANN001
        return {"pairs": [_house_party_pair()]}

    monkeypatch.setattr(tools, "_get", _fake_get)
    out = await find_divergences(min_net_edge=-1.0, limit=10, include_rules=False)
    # The house_party pair is now scanned (not dropped on orientation).
    assert out.get("orientation_excluded", 0) == 0, out
    assert out.get("pairs_scanned") == 1, out
    assert len(out.get("divergences", [])) == 1, out


async def test_scans_full_candidate_window_not_just_soonest(monkeypatch) -> None:
    """Self-oriented binary arbs resolve later than the daily-sport front, so they sit past the
    soonest-150 and were never scanned (the live 4% NHL-draft arb). The scanner must request the
    full candidate window (_DIVERGENCE_SCAN_BREADTH = the endpoint's _MAX_CANDIDATES)."""
    seen: dict = {}

    async def _fake_get(path, params, base_url):  # noqa: ANN001
        seen["limit"] = params.get("limit")
        return {"pairs": [{
            "bet_type": "event", "poly_side": None, "method": "x", "confidence": 1.0,
            "a": {"venue": "kalshi", "question": "Will GM be the 1st overall pick?",
                  "days_to_resolution": 30, "book": {"bid": 0.98, "ask": 0.99}},
            "b": {"venue": "polymarket", "question": "Will GM be the 1st overall pick?",
                  "days_to_resolution": 30, "book": {"bid": 0.93, "ask": 0.95}},
        }]}

    monkeypatch.setattr(tools, "_get", _fake_get)
    out = await find_divergences(min_net_edge=-1.0, limit=10, include_rules=False)
    assert seen["limit"] == tools._DIVERGENCE_SCAN_BREADTH   # full window, not soonest-150
    assert seen["limit"] >= 500
    assert len(out.get("divergences", [])) == 1             # the self-oriented arb surfaces


async def test_self_oriented_pass_fetches_subset_and_merges_far_arb(monkeypatch) -> None:
    """The far self-oriented arbs (elections/House) sit past the candidate cap, so the main
    soonest pass never sees them. find_divergences must fire a SECOND fetch scoped to the
    self-oriented bet types and merge it — that's what surfaces the 4% NHL-draft-class arb."""
    calls: list = []
    far_arb = {
        "bet_type": "event", "poly_side": None, "method": "x", "confidence": 1.0,
        "a": {"id": "kalshi:KXNHLDRAFTPICK", "venue": "kalshi",
              "question": "Will GM be the 1st overall pick?", "days_to_resolution": 30,
              "book": {"bid": 0.98, "ask": 0.99}},
        "b": {"id": "polymarket:1343556", "venue": "polymarket",
              "question": "Will GM be the 1st overall pick?", "days_to_resolution": 30,
              "book": {"bid": 0.93, "ask": 0.95}},
    }

    async def _fake_get(path, params, base_url):  # noqa: ANN001
        calls.append(params.get("bet_type"))
        # main pass (no bet_type) = empty perishable front; self-oriented pass returns the arb.
        return {"pairs": [far_arb]} if params.get("bet_type") else {"pairs": []}

    monkeypatch.setattr(tools, "_get", _fake_get)
    out = await find_divergences(min_net_edge=-1.0, limit=10, include_rules=False)
    # a second fetch fired, scoped to the self-oriented bet types
    assert any(c and "event" in c and "house_party" in c for c in calls), calls
    # and the far self-oriented arb surfaced (merged in from that pass)
    assert len(out.get("divergences", [])) == 1, out


def _single_pair(monkeypatch, pair):
    async def _g(path, params, base_url):  # noqa: ANN001
        # main pass returns the pair; self-oriented pass empty (avoid double-count)
        return {"pairs": [pair]} if not params.get("bet_type") else {"pairs": []}
    monkeypatch.setattr(tools, "_get", _g)


async def test_false_extreme_pinned_leg_excluded(monkeypatch) -> None:
    """A leg pinned near 1.0 with a large edge (near-resolved/stale, not lockable) is excluded —
    the Senate-pair false arb ali flagged (PM 1.0 / Kalshi 0.013, 131d out)."""
    _single_pair(monkeypatch, {
        "bet_type": "event", "poly_side": None, "method": "blocked_deterministic", "confidence": 0.7,
        "a": {"id": "kalshi:KXSENATEKSD", "venue": "kalshi", "question": "Will the Dem win KS Senate?",
              "implied_yes": 0.013, "days_to_resolution": 131, "book": {"bid": 0.01, "ask": 0.016}},
        "b": {"id": "polymarket:918307", "venue": "polymarket", "question": "Will the Dem win KS Senate?",
              "implied_yes": 1.0, "days_to_resolution": 131, "book": {"bid": 0.999, "ask": 1.0}},
    })
    out = await find_divergences(min_net_edge=-1.0, limit=10, include_rules=False)
    assert out.get("extreme_excluded", 0) >= 1, out
    assert len(out.get("divergences", [])) == 0, out


async def test_moderate_two_sided_divergence_passes(monkeypatch) -> None:
    """A moderate divergence with NO extreme leg (both legs mid-range) is the legit kind and
    must NOT be caught by the extreme guard — the KXCO8D-class real arb ali contrasted."""
    _single_pair(monkeypatch, {
        "bet_type": "event", "poly_side": None, "method": "structured_key", "confidence": 1.0,
        "a": {"id": "kalshi:KXCO8D", "venue": "kalshi", "question": "Will X happen?",
              "implied_yes": 0.55, "days_to_resolution": 5, "book": {"bid": 0.54, "ask": 0.56}},
        "b": {"id": "polymarket:704026", "venue": "polymarket", "question": "Will X happen?",
              "implied_yes": 0.25, "days_to_resolution": 5, "book": {"bid": 0.24, "ask": 0.26}},
    })
    out = await find_divergences(min_net_edge=-1.0, limit=10, include_rules=False)
    assert out.get("extreme_excluded", 0) == 0, out
    assert len(out.get("divergences", [])) == 1, out
