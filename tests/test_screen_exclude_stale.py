"""Regression tests for GET /v1/markets/screen exclude_stale (P0).

A live-trader probe found exclude_stale=true was a no-op: ~$400M of long-
resolved markets (Peru election -64d, Starmer -165d) still carry
status="active" (settle-sweep lag / bundle parents never flip), and the old
filter only dropped rows the venue already marked non-active. So the exact
markets is_stale exists to catch sailed straight through.

Handler is called directly with a fake DAO (no disk/DB).
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

from pytheum.api.markets_screen import handle_markets_screen


def _at(days_from_now: float) -> str:
    return (datetime.now(UTC) + timedelta(days=days_from_now)).isoformat()


class _StaleDao:
    """Returns crafted rows verbatim; exclude_stale is applied in the handler."""

    def __init__(self, rows: list[dict]) -> None:
        self._rows = rows

    async def screen_markets(self, **kwargs) -> list[dict]:
        return list(self._rows)


# resolution_at relative to real now drives resolution_horizon(is_stale, days).
_ROWS = [
    # long-dead but still listed active -> MUST drop under exclude_stale
    {"id": "polymarket:dead64", "question": "Peru election winner", "venue": "polymarket",
     "status": "active", "resolution_at": _at(-64), "volume_usd": 101_000_000.0},
    # venue already settled -> drop
    {"id": "polymarket:settled", "question": "Some resolved market", "venue": "polymarket",
     "status": "resolved", "resolution_at": _at(-1), "volume_usd": 5_000_000.0},
    # ended hours ago, venue still lists active (is_stale) -> MUST drop under
    # exclude_stale (a live-trader dogfood got WC games back at -0.4d despite
    # exclude_stale=true; an explicit exclude means a clean board)
    {"id": "polymarket:justended", "question": "Game last night", "venue": "polymarket",
     "status": "active", "resolution_at": _at(-0.4), "volume_usd": 2_000_000.0},
    # genuinely live -> KEEP
    {"id": "polymarket:live", "question": "Fed cut in December", "venue": "polymarket",
     "status": "active", "resolution_at": _at(+30), "volume_usd": 9_000_000.0},
]


async def test_exclude_stale_drops_long_dead_active_markets():
    dao = _StaleDao(_ROWS)
    _, body = await handle_markets_screen(
        {"status": "any", "exclude_stale": "true"}, dao=dao
    )
    ids = {m["id"] for m in body["markets"]}
    assert "polymarket:dead64" not in ids        # -64d active -> dropped
    assert "polymarket:settled" not in ids       # venue-settled -> dropped
    assert "polymarket:justended" not in ids     # -0.4d is_stale -> dropped (no grace)
    assert "polymarket:live" in ids              # live -> kept
    assert body["meta"]["dropped_stale"] == 3


async def test_no_exclude_stale_keeps_everything_with_flag():
    dao = _StaleDao(_ROWS)
    _, body = await handle_markets_screen({"status": "any"}, dao=dao)
    ids = {m["id"] for m in body["markets"]}
    assert "polymarket:dead64" in ids            # visible, just flagged
    assert body["meta"]["dropped_stale"] == 0
    dead = next(m for m in body["markets"] if m["id"] == "polymarket:dead64")
    assert dead["is_stale"] is True              # informational flag still set
