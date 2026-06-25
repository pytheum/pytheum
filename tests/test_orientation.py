"""Shared cross-venue orientation (pytheum.equivalence.orientation).

Single source of truth used both at rest (scripts/map_pair_sides → pair_side_map) and at
serve (markets_equivalents computes poly_side inline). Precision-critical: a wrong map inverts
the edge, so every helper is conservative — ambiguity returns None, never a guess.
"""
from __future__ import annotations

from pytheum.equivalence.orientation import (
    direction,
    orient_pair,
    outcomes_from_payload,
    pick_spread_side,
    pick_team_side,
    pick_total_side,
    yes_team_from_title,
)


def test_yes_team_from_title() -> None:
    assert yes_team_from_title(
        "Will Enterprise Esports win the Barça eSports vs. Enterprise Esports match?"
    ) == "Enterprise Esports"
    assert yes_team_from_title("New York Y vs Detroit Total Runs?") is None
    assert yes_team_from_title(None) is None


def test_pick_team_side_unique_and_ambiguous() -> None:
    assert pick_team_side("REBORN", ["Ghools Esports", "REBORN"]) == 1
    assert pick_team_side("Los Angeles D", ["Los Angeles Dodgers", "San Diego Padres"]) == 0  # prefix
    assert pick_team_side("", ["A", "B"]) is None


def test_direction_and_total_side() -> None:
    assert direction("Over 9.5") == "over"
    assert direction("10 or more") == "over"
    assert direction("Yes") is None
    assert pick_total_side("Over 9.5", ["Over", "Under"]) == 0
    assert pick_total_side("Under 9.5", ["Over", "Under"]) == 1
    assert pick_total_side("Yes", ["Over", "Under"]) is None  # no direction -> unmapped


def test_outcomes_from_payload() -> None:
    assert outcomes_from_payload({"outcomes": ["Over", "Under"]}) == ["Over", "Under"]
    assert outcomes_from_payload('{"outcomes": "[\\"A\\", \\"B\\"]"}') == ["A", "B"]  # nested JSON str
    assert outcomes_from_payload({"outcomes": None}) is None
    assert outcomes_from_payload(None) is None


def test_orient_pair_moneyline_from_title() -> None:
    # team-WIN: YES team from the title -> matching PM outcome (the orient-at-serve path).
    side, outcome = orient_pair(
        "esports_map",
        "Will REBORN win the Ghools Esports vs. REBORN match?",
        ["Ghools Esports", "REBORN"],
    )
    assert side == 1 and outcome == "REBORN"


def test_orient_pair_total_needs_yes_subtitle() -> None:
    # totals have no direction in the title -> need the yes_subtitle; without it, unmapped.
    assert orient_pair("total", "NYY vs DET Total Runs?", ["Over", "Under"]) == (None, None)
    assert orient_pair("total", "NYY vs DET Total Runs?", ["Over", "Under"],
                       yes_subtitle="Over 9.5") == (0, "Over")


def test_pick_spread_side_maps_yes_to_favorite() -> None:
    # Real box examples (ali): PM spread outcomes are [favorite, underdog], line in the title.
    # Kalshi YES ("<team> wins by more than N") = the favorite covering -> outcomes[0].
    assert pick_spread_side("Republic of Korea wins by more than 2.5 goals?",
                            "Spread: Korea Republic (-2.5)", ["Korea Republic", "Mexico"]) == 0
    assert pick_spread_side("Detroit wins by over 1.5 runs?",
                            "Spread: Detroit Tigers (-1.5)",
                            ["Detroit Tigers", "New York Yankees"]) == 0


def test_pick_spread_side_conservative() -> None:
    # line mismatch -> unmapped (matcher matched the line; we verify, never assume)
    assert pick_spread_side("Detroit wins by over 1.5 runs?",
                            "Spread: Detroit Tigers (-2.5)",
                            ["Detroit Tigers", "New York Yankees"]) is None
    # Kalshi YES team matches the UNDERDOG (idx 1), not the favorite -> anomalous -> unmapped
    assert pick_spread_side("Mexico wins by more than 2.5 goals?",
                            "Spread: Korea Republic (-2.5)", ["Korea Republic", "Mexico"]) is None
    # not a spread-cover title -> unmapped
    assert pick_spread_side("Will France win?", "Spread: France (-1.5)", ["France", "Iraq"]) is None
    # no line in the PM title -> unmapped
    assert pick_spread_side("Detroit wins by over 1.5 runs?", "Detroit moneyline",
                            ["Detroit Tigers", "New York Yankees"]) is None


def test_orient_pair_spread_uses_pm_title() -> None:
    assert orient_pair("spread", "France wins by more than 1.5 goals?", ["France", "Iraq"],
                       pm_title="Spread: France (-1.5)") == (0, "France")
    # without the pm_title (no line to verify) -> unmapped
    assert orient_pair("spread", "France wins by more than 1.5 goals?", ["France", "Iraq"]) == (None, None)


def test_orient_pair_conservative_and_scoped() -> None:
    # non-orientable bet type -> (None, None)
    assert orient_pair("event", "Will X win?", ["Yes", "No"]) == (None, None)
    # ambiguous team -> (None, None)
    assert orient_pair("moneyline", "no win-pattern here", ["A", "B"]) == (None, None)
    # no outcomes -> (None, None)
    assert orient_pair("moneyline", "Will A win the A vs B match?", None) == (None, None)
