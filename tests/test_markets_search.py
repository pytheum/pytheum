"""GET /v1/markets/search — non-semantic title search (t_search_markets).

Handler is exercised directly with a fake DAO (no disk/DB). Covers: hits +
ranking pass-through, venue/status filtering, empty/missing query, ref shape
normalization (market_id → id), and the degraded paths (dao=None, no search
method on the dao).
"""
from __future__ import annotations

from typing import Any

from pytheum.api.markets_search import _tokenize, handle_markets_search


class _SearchDao:
    """Records the tokens/venues/statuses it was called with and returns crafted
    rows in knn_markets shape (market_id key) — verbatim, ranked as given."""

    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows
        self.calls: list[dict[str, Any]] = []

    async def search_markets_by_title(
        self, tokens: list[str], *, venues=None, statuses=None, limit: int = 10
    ) -> list[dict[str, Any]]:
        self.calls.append({"tokens": tokens, "venues": venues,
                           "statuses": statuses, "limit": limit})
        return list(self._rows[:limit])


_ROWS = [
    {"market_id": "polymarket:558936", "question": "Super Bowl LX winner",
     "venue": "polymarket", "status": "active", "volume_usd": 5_000_000.0,
     "resolution_at": None, "payload": {"bestBid": 0.17, "bestAsk": 0.18}},
    {"market_id": "kalshi:KXSB-26", "question": "Super Bowl LX champion",
     "venue": "kalshi", "status": "active", "volume_usd": 1_000_000.0,
     "resolution_at": None, "payload": {"bestBid": 0.40, "bestAsk": 0.42}},
]


def test_tokenize_caps_and_drops_empties() -> None:
    assert _tokenize("super bowl") == ["super", "bowl"]
    assert _tokenize("   ") == []
    # caps at 4 tokens (mirrors the DAO's AND-match cap)
    assert _tokenize("a b c d e f") == ["a", "b", "c", "d"]


async def test_search_hits_and_shape() -> None:
    dao = _SearchDao(_ROWS)
    code, body = await handle_markets_search({"q": "super bowl"}, dao=dao)
    assert code == 200
    assert body["count"] == 2
    # tokens passed through to the DAO AND-match
    assert dao.calls[0]["tokens"] == ["super", "bowl"]
    # market_id is normalized to id, book is hydrated from payload
    first = body["markets"][0]
    assert first["id"] == "polymarket:558936"
    assert first["book"] and first["book"]["bid"] == 0.17 and first["book"]["ask"] == 0.18
    assert body["meta"]["query"] == "super bowl"
    assert body["meta"]["tokens"] == ["super", "bowl"]


async def test_search_ranking_preserved_from_dao() -> None:
    # The DAO ranks by volume desc; the handler must not re-order.
    dao = _SearchDao(_ROWS)
    _, body = await handle_markets_search({"q": "super"}, dao=dao)
    ids = [m["id"] for m in body["markets"]]
    assert ids == ["polymarket:558936", "kalshi:KXSB-26"]


async def test_search_venue_and_status_filter_forwarded() -> None:
    dao = _SearchDao(_ROWS)
    await handle_markets_search(
        {"q": "super bowl", "venues": "kalshi", "status": "resolved"}, dao=dao)
    assert dao.calls[0]["venues"] == ["kalshi"]
    assert dao.calls[0]["statuses"] == ["resolved"]


async def test_search_status_any_clears_filter() -> None:
    dao = _SearchDao(_ROWS)
    await handle_markets_search({"q": "super", "status": "any"}, dao=dao)
    assert dao.calls[0]["statuses"] is None


async def test_search_limit_capped() -> None:
    dao = _SearchDao(_ROWS)
    await handle_markets_search({"q": "super", "limit": "9999"}, dao=dao)
    assert dao.calls[0]["limit"] == 200  # MAX_LIMIT


async def test_search_empty_when_no_match() -> None:
    dao = _SearchDao([])
    code, body = await handle_markets_search({"q": "nonexistent"}, dao=dao)
    assert code == 200
    assert body["count"] == 0
    assert body["markets"] == []


async def test_search_missing_query_returns_structured_error() -> None:
    code, body = await handle_markets_search({}, dao=_SearchDao(_ROWS))
    assert code == 200
    assert body["count"] == 0
    assert body["meta"]["error"] == "missing_query"


async def test_search_no_dao_degrades_not_errors() -> None:
    code, body = await handle_markets_search({"q": "super"}, dao=None)
    assert code == 200
    assert body["meta"]["degraded"] is True
    assert body["meta"]["degraded_reason"] == "db_unavailable"


async def test_search_dao_without_method_degrades() -> None:
    class _BareDao:
        pass

    code, body = await handle_markets_search({"q": "super"}, dao=_BareDao())
    assert code == 200
    assert body["meta"]["degraded_reason"] == "search_unavailable"
