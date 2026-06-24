"""Row mapper for the missing-Kalshi-leg fetch-list (scripts.sync_paired_kalshi).

Closes ali's finding #2 Kalshi-side gap: sync_paired_polymarket only fills PM rows where
the Kalshi leg already exists, so freshly-wired clusters (KXATPGTOTAL tennis) whose Kalshi
leg is missing never surface. This maps matcher-DB Kalshi rows into serving markets rows.
"""

from __future__ import annotations

import json

from scripts.sync_paired_kalshi import kalshi_row_to_market


def test_maps_id_status_resolution_and_book() -> None:
    raw = json.dumps({
        "yes_bid": 42, "yes_ask": 45, "last_price": 43,
        "event_ticker": "KXATPGTOTAL-26JUN22BERMUN", "volume_fp": "1200",
        "liquidity_dollars": "50", "rules_primary": "Settles YES if total games > 24.",
    })
    row = kalshi_row_to_market(
        "KXATPGTOTAL-26JUN22BERMUN-24", "Total games over 24.5?", "active",
        "2026-06-22T18:00:00Z", raw)
    assert row is not None
    assert row[0] == "kalshi:KXATPGTOTAL-26JUN22BERMUN-24"  # serving id
    assert row[2] == "active"  # status mapped
    assert row[6] is not None and row[6].year == 2026  # resolution_at parsed
    payload = json.loads(row[7])
    # Kalshi cents -> [0,1] floats the serving book_from_payload reader expects
    assert payload["bestBid"] == 0.42
    assert payload["bestAsk"] == 0.45
    assert payload["lastTradePrice"] == 0.43
    assert payload["synced_by"] == "kalshi_supplemental"  # reversible marker
    assert row[3] == 1200.0  # volume_usd
    assert row[4] == 50.0    # liquidity_usd


def test_non_active_status_maps_to_closed_and_no_book_without_raw() -> None:
    row = kalshi_row_to_market("KXFOO-1", "t", "finalized", None, None)
    assert row is not None
    assert row[2] == "closed"
    assert row[6] is None  # no close_date / raw -> no resolution
    payload = json.loads(row[7])
    assert "bestBid" not in payload  # no raw book fields
    assert payload["synced_by"] == "kalshi_supplemental"


def test_empty_ticker_returns_none() -> None:
    assert kalshi_row_to_market("", None, None, None, None) is None
