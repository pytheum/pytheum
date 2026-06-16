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
