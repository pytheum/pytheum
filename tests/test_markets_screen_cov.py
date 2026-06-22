"""Coverage tests for pytheum.api.markets_screen.

Exercises the helpers (_num, _dt), the bundle-top-outcome attach (no-method
guard, no-event-ids, exception swallow, no-children, no-priced-children, full
ladder), the move-sort pool path, exclude_stale dropping, and the degraded
no-DAO path.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from pytheum.api.markets_screen import (
    _attach_bundle_top_outcome,
    _dt,
    _num,
    handle_markets_screen,
)

# --------------------------------------------------------------------------- #
# _num / _dt
# --------------------------------------------------------------------------- #


def test_num_parses_and_guards() -> None:
    assert _num("12.5") == 12.5
    assert _num(None) is None
    assert _num("") is None
    assert _num("abc") is None


def test_dt_parses_and_guards() -> None:
    assert _dt(None) is None
    assert _dt("") is None
    assert _dt("not-a-date") is None
    d = _dt("2026-07-01T00:00:00Z")
    assert d is not None and d.tzinfo is not None
    # date-only string gains a UTC tzinfo
    d2 = _dt("2026-07-01")
    assert d2 is not None and d2.tzinfo is not None


# --------------------------------------------------------------------------- #
# Fake DAOs
# --------------------------------------------------------------------------- #


class _ScreenDao:
    """DAO that only implements screen_markets (no enrichment methods)."""

    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows
        self.last_kwargs: dict[str, Any] = {}

    async def screen_markets(self, **kwargs: Any) -> list[dict[str, Any]]:
        self.last_kwargs = kwargs
        return self._rows


class _ChildrenDao(_ScreenDao):
    def __init__(
        self,
        rows: list[dict[str, Any]],
        children: dict[str, list[dict[str, Any]]],
        *,
        raises: bool = False,
    ) -> None:
        super().__init__(rows)
        self._children = children
        self._raises = raises

    async def fetch_children_for_events(
        self, event_ids: list[str]
    ) -> dict[str, list[dict[str, Any]]]:
        if self._raises:
            raise RuntimeError("boom")
        return self._children

    async def fetch_moves(self, ids: list[str]) -> dict[str, Any]:
        return {}


def _market(
    mid: str, venue: str = "polymarket", *, implied: bool = True, **over: Any
) -> dict[str, Any]:
    row: dict[str, Any] = {
        "id": mid,
        "question": over.pop("question", f"q-{mid}"),
        "venue": venue,
        "status": over.pop("status", "active"),
        "volume_usd": over.pop("volume_usd", 1000.0),
        "liquidity_usd": 500.0,
        "url": None,
        "resolution_at": over.pop("resolution_at", None),
        "payload": over.pop(
            "payload",
            {"outcomePrices": "[0.6, 0.4]"} if implied else {},
        ),
    }
    row.update(over)
    return row


# --------------------------------------------------------------------------- #
# handle_markets_screen — degraded
# --------------------------------------------------------------------------- #


async def test_screen_no_dao_degrades() -> None:
    status, body = await handle_markets_screen({}, dao=None)
    assert status == 200
    assert body["markets"] == []
    assert body["meta"]["degraded"] is True
    assert body["meta"]["degraded_reason"] == "db_unavailable"


async def test_screen_basic_rows() -> None:
    dao = _ScreenDao([_market("polymarket:1"), _market("polymarket:2")])
    status, body = await handle_markets_screen({}, dao=dao)
    assert status == 200
    assert body["count"] == 2
    # `venue` param alias is accepted and forwarded.
    assert dao.last_kwargs["sort_by"] == "volume"


async def test_screen_venue_alias_and_status_any() -> None:
    dao = _ScreenDao([_market("kalshi:1", "kalshi")])
    _, body = await handle_markets_screen(
        {"venue": "kalshi", "status": "any", "sort_by": "bogus"}, dao=dao
    )
    assert dao.last_kwargs["venues"] == ["kalshi"]
    assert dao.last_kwargs["status"] is None  # 'any' → no filter
    # invalid sort_by falls back to volume in the DAO call
    assert dao.last_kwargs["sort_by"] == "volume"
    assert body["meta"]["filters"]["sort_by"] == "volume"


async def test_screen_exclude_stale_drops_past_resolution() -> None:
    past = (datetime.now(UTC) - timedelta(days=2)).isoformat()
    future = (datetime.now(UTC) + timedelta(days=5)).isoformat()
    dao = _ScreenDao([
        _market("polymarket:stale", resolution_at=past),
        _market("polymarket:live", resolution_at=future),
    ])
    _, body = await handle_markets_screen({"exclude_stale": "true"}, dao=dao)
    ids = [m["id"] for m in body["markets"]]
    assert "polymarket:live" in ids
    assert "polymarket:stale" not in ids
    assert body["meta"]["dropped_stale"] == 1


async def test_screen_resolution_at_isoformat_datetime() -> None:
    future_dt = datetime.now(UTC) + timedelta(days=3)
    dao = _ScreenDao([_market("polymarket:1", resolution_at=future_dt)])
    _, body = await handle_markets_screen({}, dao=dao)
    # datetime is serialized to ISO string in the row.
    assert body["markets"][0]["resolution_at"] == future_dt.isoformat()


# --------------------------------------------------------------------------- #
# sort_by=move path
# --------------------------------------------------------------------------- #


async def test_screen_move_sort_ranks_by_abs_move() -> None:
    rows = [_market("polymarket:a"), _market("polymarket:b")]

    class _MoveDao(_ScreenDao):
        async def fetch_moves(self, ids: list[str]) -> dict[str, Any]:
            return {"polymarket:a": {"move_24h": 0.01}, "polymarket:b": {"move_24h": -0.5}}

    dao = _MoveDao(rows)
    _, body = await handle_markets_screen({"sort_by": "move", "limit": "5"}, dao=dao)
    # The volume pool is fetched first (move col lives in price tables).
    assert dao.last_kwargs["sort_by"] == "volume"
    # Largest |move_24h| first.
    assert body["markets"][0]["id"] == "polymarket:b"
    assert body["meta"]["filters"]["sort_by"] == "move"


# --------------------------------------------------------------------------- #
# _attach_bundle_top_outcome
# --------------------------------------------------------------------------- #


async def test_bundle_no_method_noop() -> None:
    markets = [_market("polymarket:1", implied=False)]
    markets[0]["implied_yes"] = None
    await _attach_bundle_top_outcome(markets, dao=_ScreenDao([]))
    assert "bundle_top_outcome" not in markets[0]


async def test_bundle_no_event_ids_noop() -> None:
    # implied_yes present → not a bundle parent → no event ids collected.
    m = _market("polymarket:1")
    m["implied_yes"] = 0.5
    dao = _ChildrenDao([], {})
    await _attach_bundle_top_outcome([m], dao=dao)
    assert "bundle_top_outcome" not in m


async def test_bundle_fetch_raises_swallowed() -> None:
    m = _market("polymarket:99")
    m["implied_yes"] = None
    dao = _ChildrenDao([], {}, raises=True)
    await _attach_bundle_top_outcome([m], dao=dao)
    assert "bundle_top_outcome" not in m


async def test_bundle_no_children_leaves_untouched() -> None:
    m = _market("polymarket:99")
    m["implied_yes"] = None
    dao = _ChildrenDao([], {"99": []})  # no sibling legs
    await _attach_bundle_top_outcome([m], dao=dao)
    assert "bundle_top_outcome" not in m


async def test_bundle_children_present_but_unpriced() -> None:
    m = _market("polymarket:99")
    m["implied_yes"] = None
    # child has empty payload → no implied_yes → ladder empty.
    kid = {"id": "polymarket:100", "question": "Child", "payload": {}, "volume_usd": 10.0}
    dao = _ChildrenDao([], {"99": [kid]})
    await _attach_bundle_top_outcome([m], dao=dao)
    assert m["bundle_top_outcome"] is None
    assert m["bundle_top_reason"] == "no_priced_children"


async def test_bundle_full_ladder() -> None:
    m = _market("polymarket:99")
    m["implied_yes"] = None
    kids = [
        {"id": "polymarket:100", "question": "Waller",
         "payload": {"outcomePrices": "[0.6, 0.4]", "group_item_title": "Waller"},
         "volume_usd": 100.0},
        {"id": "polymarket:101", "question": "Warsh",
         "payload": {"outcomePrices": "[0.3, 0.7]", "group_item_title": "Warsh"},
         "volume_usd": 50.0},
    ]
    dao = _ChildrenDao([], {"99": kids})
    await _attach_bundle_top_outcome([m], dao=dao)
    assert m["bundle_top_outcome"]["outcome"] == "Waller"
    assert m["bundle_top_outcome"]["implied_yes"] == 0.6
    assert len(m["bundle_outcomes"]) == 2


async def test_bundle_skips_non_polymarket_and_no_colon() -> None:
    # Kalshi parent with implied None and an id w/o colon → skipped from event_ids.
    m = _market("kalshi-nocolon", "kalshi")
    m["id"] = "nocolon"
    m["implied_yes"] = None
    dao = _ChildrenDao([], {})
    await _attach_bundle_top_outcome([m], dao=dao)
    assert "bundle_top_outcome" not in m


async def test_screen_full_pipeline_with_children_dao() -> None:
    """End-to-end: a bundle parent (implied None) gets a ladder via the
    children DAO, and ordinary rows pass through enrichment cleanly."""
    parent = _market("polymarket:200", implied=False)
    parent["payload"] = {}
    kids = [
        {"id": "polymarket:201", "question": "Spain",
         "payload": {"outcomePrices": "[0.17, 0.83]", "group_item_title": "Spain"},
         "volume_usd": 100.0},
    ]
    dao = _ChildrenDao([parent], {"200": kids})
    status, body = await handle_markets_screen({}, dao=dao)
    assert status == 200
    assert body["markets"][0]["bundle_top_outcome"]["outcome"] == "Spain"
