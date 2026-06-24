"""Row mapper for the missing-Kalshi-leg fetch-list (scripts.sync_paired_kalshi).

Closes ali's finding #2 Kalshi-side gap: sync_paired_polymarket only fills PM rows where
the Kalshi leg already exists, so freshly-wired clusters (KXATPGTOTAL tennis) whose Kalshi
leg is missing never surface. This maps matcher-DB Kalshi rows into serving markets rows.
"""

from __future__ import annotations

import gzip
import json
from pathlib import Path

from scripts.sync_paired_kalshi import (
    _load_export_rows,
    export_row_to_market,
    kalshi_row_to_market,
)


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


# --- box-side --from-export source ---

def test_export_row_active_when_future() -> None:
    row = export_row_to_market(
        "kalshi:KXATPGTOTAL-26JUN30X", "Total games over 24.5?", None, "2026-06-30", "2026-06-23")
    assert row is not None
    assert row[0] == "kalshi:KXATPGTOTAL-26JUN30X"  # serving id (already kalshi:…)
    assert row[1] == "Total games over 24.5?"
    assert row[2] == "active"
    assert row[6] is not None and row[6].year == 2026  # resolution_at seeded
    payload = json.loads(row[7])
    assert payload["synced_by"] == "kalshi_supplemental"
    assert payload["source"] == "export"


def test_export_row_closed_when_past() -> None:
    row = export_row_to_market("kalshi:KXOLD", "t", None, "2026-01-01", "2026-06-23")
    assert row is not None and row[2] == "closed"


def test_export_row_game_date_overrides_lagged_resolution() -> None:
    # The regression: game_date past (match yesterday) but Kalshi resolution lags 2 weeks.
    # game_date wins -> closed + resolution_at seeded from game_date, not the lagged date.
    row = export_row_to_market("kalshi:KXATPGTOTAL-26JUN22X", "t",
                               "2026-06-22", "2026-07-06", "2026-06-23")
    assert row is not None
    assert row[2] == "closed"  # past game, NOT active off the lagged 07-06
    assert row[6] is not None and row[6].day == 22  # resolution_at = game_date 06-22


def test_export_row_rejects_non_kalshi_ref() -> None:
    assert export_row_to_market("polymarket:123", "t", None, "2026-06-30", "2026-06-23") is None
    assert export_row_to_market(None, "t", None, None, "2026-06-23") is None


def test_load_export_rows_filters_to_missing(tmp_path: Path) -> None:
    p = tmp_path / "exp.jsonl.gz"
    with gzip.open(p, "wt", encoding="utf-8") as fh:
        for ref, title in (("kalshi:A", "TA"), ("kalshi:B", "TB")):
            fh.write(json.dumps({"kalshi_ref": ref, "kalshi_title": title,
                                 "resolution_date": "2026-06-30"}) + "\n")
    out = _load_export_rows(str(p), {"kalshi:A"}, "2026-06-23", None)
    assert set(out) == {"kalshi:A"}  # only the requested-missing id
    assert out["kalshi:A"][1] == "TA"


def test_load_export_rows_live_only_uses_game_date(tmp_path: Path) -> None:
    p = tmp_path / "exp.jsonl.gz"
    with gzip.open(p, "wt", encoding="utf-8") as fh:
        # LIVE: future game. PASTGAME: the bug — past game_date but lagged future resolution.
        # EVENT: no game_date, future resolution (events keep resolution_date). UNDATED: skip.
        fh.write(json.dumps({"kalshi_ref": "kalshi:LIVE", "kalshi_title": "l",
                             "game_date": "2026-06-30", "resolution_date": "2026-06-30"}) + "\n")
        fh.write(json.dumps({"kalshi_ref": "kalshi:PASTGAME", "kalshi_title": "p",
                             "game_date": "2026-06-22", "resolution_date": "2026-07-06"}) + "\n")
        fh.write(json.dumps({"kalshi_ref": "kalshi:EVENT", "kalshi_title": "e",
                             "resolution_date": "2026-11-03"}) + "\n")
        fh.write(json.dumps({"kalshi_ref": "kalshi:UNDATED", "kalshi_title": "u"}) + "\n")
    out = _load_export_rows(str(p), {"kalshi:LIVE", "kalshi:PASTGAME", "kalshi:EVENT",
                                     "kalshi:UNDATED"}, "2026-06-23", "2026-06-23")
    # PASTGAME excluded (game_date past despite lagged resolution); UNDATED excluded.
    assert set(out) == {"kalshi:LIVE", "kalshi:EVENT"}
