"""Coverage tests for pytheum.api.annotators — serve-safe row mutators.

Exercises every branch of attach_quote_staleness / attach_moves /
attach_cross_venue (no-method guard, empty-ids guard, exception swallow, the
parked-wall classification) and reserve_cross_venue_slot's swap algorithm.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from pytheum.api.annotators import (
    attach_cross_venue,
    attach_moves,
    attach_quote_staleness,
    reserve_cross_venue_slot,
)

# --------------------------------------------------------------------------- #
# Fake DAOs
# --------------------------------------------------------------------------- #


class _NoMethodDao:
    """A minimal DAO that lacks all enrichment methods."""


class _LastMoveDao:
    def __init__(self, mapping: dict[str, Any], *, raises: bool = False) -> None:
        self._mapping = mapping
        self._raises = raises

    async def fetch_last_move_at(self, ids: list[str]) -> dict[str, Any]:
        if self._raises:
            raise RuntimeError("boom")
        return self._mapping


class _MovesDao:
    def __init__(self, mapping: dict[str, Any], *, raises: bool = False) -> None:
        self._mapping = mapping
        self._raises = raises

    async def fetch_moves(self, ids: list[str]) -> dict[str, Any]:
        if self._raises:
            raise RuntimeError("boom")
        return self._mapping


class _TwinsDao:
    def __init__(self, mapping: dict[str, Any], *, raises: bool = False) -> None:
        self._mapping = mapping
        self._raises = raises

    async def fetch_equivalents_for_ids(self, ids: list[str]) -> dict[str, Any]:
        if self._raises:
            raise RuntimeError("boom")
        return self._mapping


# --------------------------------------------------------------------------- #
# attach_quote_staleness
# --------------------------------------------------------------------------- #


async def test_quote_staleness_no_method_noop() -> None:
    rows = [{"id": "a"}]
    await attach_quote_staleness(rows, dao=_NoMethodDao())
    assert "last_move_age_s" not in rows[0]


async def test_quote_staleness_empty_ids_noop() -> None:
    rows = [{"id": None}, {}]  # no usable id_key values
    await attach_quote_staleness(rows, dao=_LastMoveDao({}))
    assert all("last_move_age_s" not in r for r in rows)


async def test_quote_staleness_fetch_raises_swallowed() -> None:
    rows = [{"id": "a"}]
    await attach_quote_staleness(rows, dao=_LastMoveDao({}, raises=True))
    assert "last_move_age_s" not in rows[0]


async def test_quote_staleness_marks_parked_wall() -> None:
    old = datetime.now(UTC) - timedelta(hours=10)
    rows = [{
        "id": "a",
        "book": {"spread": 0.005},
        "status": "Active",
    }]
    await attach_quote_staleness(rows, dao=_LastMoveDao({"a": old}))
    assert rows[0]["last_move_age_s"] > 6 * 3600
    assert rows[0]["is_parked_wall"] is True


async def test_quote_staleness_naive_datetime_assumed_utc() -> None:
    # tz-naive last-move stamp must be treated as UTC, not crash.
    old_naive = (datetime.now(UTC) - timedelta(hours=10)).replace(tzinfo=None)
    rows = [{"id": "a", "book": {"spread": 0.005}, "status": "active"}]
    await attach_quote_staleness(rows, dao=_LastMoveDao({"a": old_naive}))
    assert rows[0]["is_parked_wall"] is True


async def test_quote_staleness_not_parked_when_recent() -> None:
    recent = datetime.now(UTC) - timedelta(minutes=5)
    rows = [{"id": "a", "book": {"spread": 0.005}, "status": "active"}]
    await attach_quote_staleness(rows, dao=_LastMoveDao({"a": recent}))
    assert rows[0]["is_parked_wall"] is False


async def test_quote_staleness_not_parked_when_wide_spread() -> None:
    old = datetime.now(UTC) - timedelta(hours=10)
    rows = [{"id": "a", "book": {"spread": 0.10}, "status": "active"}]
    await attach_quote_staleness(rows, dao=_LastMoveDao({"a": old}))
    assert rows[0]["is_parked_wall"] is False


async def test_quote_staleness_not_parked_when_inactive() -> None:
    old = datetime.now(UTC) - timedelta(hours=10)
    rows = [{"id": "a", "book": {"spread": 0.005}, "status": "settled"}]
    await attach_quote_staleness(rows, dao=_LastMoveDao({"a": old}))
    assert rows[0]["is_parked_wall"] is False


async def test_quote_staleness_skips_rows_without_move() -> None:
    rows = [{"id": "a", "book": {"spread": 0.005}, "status": "active"}]
    # mapping returns None for the id → row is skipped (continue branch)
    await attach_quote_staleness(rows, dao=_LastMoveDao({"a": None}))
    assert "last_move_age_s" not in rows[0]


async def test_quote_staleness_missing_book_spread() -> None:
    old = datetime.now(UTC) - timedelta(hours=10)
    rows = [{"id": "a", "status": "active"}]  # no book → spread is None
    await attach_quote_staleness(rows, dao=_LastMoveDao({"a": old}))
    assert rows[0]["is_parked_wall"] is False


async def test_quote_staleness_custom_id_key() -> None:
    old = datetime.now(UTC) - timedelta(hours=10)
    rows = [{"market_id": "m1", "book": {"spread": 0.005}, "status": "active"}]
    await attach_quote_staleness(
        rows, dao=_LastMoveDao({"m1": old}), id_key="market_id"
    )
    assert rows[0]["is_parked_wall"] is True


# --------------------------------------------------------------------------- #
# attach_moves
# --------------------------------------------------------------------------- #


async def test_moves_no_method_noop() -> None:
    rows = [{"id": "a"}]
    await attach_moves(rows, dao=_NoMethodDao())
    assert "move_24h" not in rows[0]


async def test_moves_empty_ids_noop() -> None:
    rows = [{}]
    await attach_moves(rows, dao=_MovesDao({"a": {"move_24h": 1.0}}))
    assert rows[0] == {}


async def test_moves_fetch_raises_swallowed() -> None:
    rows = [{"id": "a"}]
    await attach_moves(rows, dao=_MovesDao({}, raises=True))
    assert "move_24h" not in rows[0]


async def test_moves_updates_rows() -> None:
    rows = [{"id": "a"}, {"id": "b"}]
    await attach_moves(rows, dao=_MovesDao({"a": {"move_24h": 0.05, "move_7d": 0.1}}))
    assert rows[0]["move_24h"] == 0.05
    assert rows[0]["move_7d"] == 0.1
    assert "move_24h" not in rows[1]  # falsy mapping value → no update


# --------------------------------------------------------------------------- #
# attach_cross_venue
# --------------------------------------------------------------------------- #


async def test_cross_venue_no_method_noop() -> None:
    rows = [{"id": "a"}]
    await attach_cross_venue(rows, dao=_NoMethodDao())
    assert "cross_venue" not in rows[0]


async def test_cross_venue_empty_ids_noop() -> None:
    rows = [{}]
    await attach_cross_venue(rows, dao=_TwinsDao({"a": {"market_id": "x"}}))
    assert "cross_venue" not in rows[0]


async def test_cross_venue_fetch_raises_swallowed() -> None:
    rows = [{"id": "a"}]
    await attach_cross_venue(rows, dao=_TwinsDao({}, raises=True))
    assert "cross_venue" not in rows[0]


async def test_cross_venue_attaches_twin() -> None:
    rows = [{"id": "a"}, {"id": "b"}]
    twin = {"market_id": "polymarket:1", "method": "structured_key", "confidence": 1.0}
    await attach_cross_venue(rows, dao=_TwinsDao({"a": twin}))
    assert rows[0]["cross_venue"]["market_id"] == "polymarket:1"
    assert "cross_venue" not in rows[1]


async def test_cross_venue_tags_settlement_fungibility() -> None:
    """Each twin is flagged fungible (deterministic/structural) vs not (LLM-judged)
    so an agent knows whether a cross-venue spread is a real lock or needs a rules
    check (the strict-threshold-vs-touch settlement trap)."""
    rows = [{"id": "det"}, {"id": "judged"}]
    twins = {
        "det": {"market_id": "polymarket:1", "method": "structured_key", "confidence": 1.0},
        "judged": {"market_id": "polymarket:2", "method": "opus_backstop", "confidence": 0.9},
    }
    await attach_cross_venue(rows, dao=_TwinsDao(twins))
    assert rows[0]["cross_venue"]["fungible"] is True   # deterministic/structural
    assert rows[1]["cross_venue"]["fungible"] is False  # LLM-judged → confirm rules


# --------------------------------------------------------------------------- #
# reserve_cross_venue_slot
# --------------------------------------------------------------------------- #


def _row(venue: str, n: int) -> dict[str, Any]:
    return {"id": f"{venue}:{n}", "venue": venue}


def test_reserve_noop_when_limit_lt_2() -> None:
    rows = [_row("polymarket", 1), _row("kalshi", 2)]
    assert reserve_cross_venue_slot(rows, limit=1) is rows


def test_reserve_noop_when_fewer_rows_than_limit() -> None:
    rows = [_row("polymarket", 1)]
    assert reserve_cross_venue_slot(rows, limit=5) is rows


def test_reserve_noop_when_no_missing_venue() -> None:
    # top-K already contains kalshi+polymarket; nothing missing in the tail.
    rows = [
        _row("polymarket", 1),
        _row("kalshi", 2),
        _row("polymarket", 3),
        _row("polymarket", 4),
    ]
    out = reserve_cross_venue_slot(rows, limit=2)
    assert out is rows


def test_reserve_swaps_missing_venue_into_topk() -> None:
    # limit=2, top-2 is all polymarket; a kalshi row sits in the tail and must
    # be swapped into the bottom slot of top-K.
    rows = [
        _row("polymarket", 1),
        _row("polymarket", 2),
        _row("polymarket", 3),
        _row("kalshi", 9),
    ]
    out = reserve_cross_venue_slot(rows, limit=2)
    venues = {r["venue"] for r in out[:2]}
    assert venues == {"polymarket", "kalshi"}
    # top spot stays natural (the #1 polymarket row).
    assert out[0]["id"] == "polymarket:1"
    assert out[1]["id"] == "kalshi:9"


def test_reserve_ignores_rows_without_venue() -> None:
    rows = [
        _row("polymarket", 1),
        _row("polymarket", 2),
        {"id": "novenue", "venue": None},
        {"id": "novenue2"},
    ]
    out = reserve_cross_venue_slot(rows, limit=2)
    # No real missing venue in the tail → unchanged.
    assert out is rows
