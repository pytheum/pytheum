"""Live-only scoping for the PM backfill (scripts.sync_paired_polymarket).

The fill path must scope to genuinely-live pairs (effective date = game_date else
resolution_date >= min_date) so it doesn't fetch ~100k historical legs from Gamma. Mirrors
the Kalshi backfill's game_date-aware live-only filter. Pure helpers, no DB/Gamma needed.
"""
from __future__ import annotations

import gzip
import json
from pathlib import Path

from scripts.sync_paired_polymarket import _effective_date, _live_pm_refs


def test_effective_date_prefers_game_date() -> None:
    # game_date wins over a lagged resolution_date (sports: Kalshi close lags the event).
    assert _effective_date("2026-06-22", "2026-07-06") == "2026-06-22"
    # events have no game_date -> fall back to resolution_date.
    assert _effective_date(None, "2026-11-03") == "2026-11-03"
    assert _effective_date(None, None) is None


def test_live_pm_refs_keeps_only_live(tmp_path: Path) -> None:
    p = tmp_path / "exp.jsonl.gz"
    with gzip.open(p, "wt", encoding="utf-8") as fh:
        # LIVE: future game. PASTGAME: past game_date despite lagged future resolution (excluded).
        # EVENT: no game_date, future resolution (kept). OLD: past resolution (excluded). NOREF: skip.
        fh.write(json.dumps({"pm_ref": "polymarket:1", "game_date": "2026-06-30",
                             "resolution_date": "2026-06-30"}) + "\n")
        fh.write(json.dumps({"pm_ref": "polymarket:2", "game_date": "2026-06-22",
                             "resolution_date": "2026-07-06"}) + "\n")
        fh.write(json.dumps({"pm_ref": "polymarket:3", "resolution_date": "2026-11-03"}) + "\n")
        fh.write(json.dumps({"pm_ref": "polymarket:4", "resolution_date": "2026-01-01"}) + "\n")
        fh.write(json.dumps({"resolution_date": "2026-12-01"}) + "\n")  # no pm_ref
    live = _live_pm_refs(str(p), "2026-06-23")
    assert live == {"polymarket:1", "polymarket:3"}  # past-game + old + no-ref excluded
