"""bet_type filter on _index_rows_to_pairs (the dedicated self-oriented fetch).

The self-oriented binary pairs (event/house_party) resolve far out, so they sit PAST the
soonest-front scan_budget — a normal page never reaches them. The bet_type filter must scan the
FULL corpus for the subset (bypassing scan_budget) so they surface for the divergence scanner.
"""
from __future__ import annotations

from pytheum.api.markets_equivalents import _index_rows_to_pairs


def _rows() -> list[dict]:
    # 50 moneyline rows, then a lone event row PAST a small scan_budget.
    rows = [{"kalshi_ref": f"kalshi:S{i}", "pm_ref": f"polymarket:{i}",
             "bet_type": "moneyline", "method": "m", "resolution_date": "2099-01-01"}
            for i in range(50)]
    rows.append({"kalshi_ref": "kalshi:EVT", "pm_ref": "polymarket:evt",
                 "bet_type": "event", "method": "m", "resolution_date": "2099-01-01"})
    return rows


def test_without_filter_far_row_past_budget_is_missed() -> None:
    pairs = _index_rows_to_pairs(_rows(), limit=100, scan_budget=10, skip_row_stale=False)
    assert not any(p["bet_type"] == "event" for p in pairs)  # budget stops before the event row


def test_bet_type_filter_bypasses_budget_and_finds_subset() -> None:
    pairs = _index_rows_to_pairs(_rows(), limit=100, scan_budget=10, skip_row_stale=False,
                                 bet_types=frozenset({"event"}))
    assert [p["kalshi_market_id"] for p in pairs] == ["kalshi:EVT"]  # full-corpus scan, subset only


def test_bet_type_filter_respects_limit() -> None:
    rows = [{"kalshi_ref": f"kalshi:E{i}", "pm_ref": f"polymarket:{i}",
             "bet_type": "house_party", "method": "m", "resolution_date": "2099-01-01"}
            for i in range(20)]
    pairs = _index_rows_to_pairs(rows, limit=5, scan_budget=3, skip_row_stale=False,
                                 bet_types=frozenset({"house_party"}))
    assert len(pairs) == 5  # limit honored even though it scans past scan_budget for the subset
