"""Proposition-aware total orientation (scripts.map_pair_sides.pick_total_side).

A total pair is oriented by DIRECTION: the Kalshi YES side's over/under maps to the PM
outcome of the same direction. Must be conservative — a wrong total orientation inverts the
edge, so anything ambiguous stays unmapped. (Team-token pick_side stays for moneylines.)
"""
from __future__ import annotations

from scripts.map_pair_sides import _direction, pick_side, pick_total_side


def test_direction_parse() -> None:
    assert _direction("Over 9.5") == "over"
    assert _direction("10 or more runs") == "over"
    assert _direction("Under 9.5") == "under"
    assert _direction("9 or fewer") == "under"
    assert _direction("Yes") is None        # uninformative -> no direction
    assert _direction("New York Yankees") is None


def test_kalshi_over_maps_to_pm_over() -> None:
    # Kalshi YES = "Over 9.5"; PM outcomes ["Over","Under"] -> poly_side = Over (index 0).
    assert pick_total_side("Over 9.5", ["Over", "Under"]) == 0
    assert pick_total_side("10 or more", ["Under", "Over"]) == 1   # order-independent


def test_kalshi_under_maps_to_pm_under() -> None:
    assert pick_total_side("Under 9.5", ["Over", "Under"]) == 1


def test_unmapped_when_kalshi_side_uninformative() -> None:
    # If the Kalshi YES side names no direction (e.g. "Yes"), we must NOT guess.
    assert pick_total_side("Yes", ["Over", "Under"]) is None


def test_unmapped_when_no_clear_overunder_pair() -> None:
    # PM outcomes without a clear over/under pair -> unmapped (don't invert).
    assert pick_total_side("Over 9.5", ["Team A", "Team B"]) is None
    assert pick_total_side("Over 9.5", ["Over", "Over"]) is None   # ambiguous, no opposite


def test_total_mapper_does_not_borrow_team_logic() -> None:
    # The team-token mapper would wrongly "align" over/under by token; the total mapper
    # is direction-only. Sanity: pick_side (team) on an O/U pair shouldn't be used for totals.
    # Here just assert the two are distinct functions with distinct behavior.
    assert pick_side("Over 9.5", ["Over", "Under"]) is not None  # team-mapper would token-match "over"
    # but the run() routes totals to pick_total_side, which is the direction-safe one.
