"""Data-serving API route registration (Group A + Group B).

``register_all(registry, *, dao, ...)``
    Registers Group A routes: status, equivalents (collection + per-ref),
    matched, rules, related.  Intended to be the first call in the wiring
    sequence; Group B (trader/venue_stream/mcp) appends after.

``register_group_B(registry, *, clients, dao=None)``
    Registers Group B routes: markets/screen, per-market trader-data
    (book/trades/oi/ohlcv/holders), whale-trades, traders leaderboard +
    profile, and the /screen endpoint.

Handler closures capture ``dao``, ``equivalence``, ``related``, and
``clients`` at registration time so the Router can call them as positional-
only callables (``handler(*path_args, query)``).

Boundary: this module must NOT import from pytheum_pit (enforced by
.importlinter).
"""
from __future__ import annotations

from typing import Any

from pytheum.api.markets_book import handle_market_book
from pytheum.api.markets_equivalents import handle_market_equivalents, handle_markets_equivalents
from pytheum.api.markets_holders import handle_market_holders
from pytheum.api.markets_matched import handle_markets_matched
from pytheum.api.markets_ohlcv import handle_market_ohlcv
from pytheum.api.markets_oi import handle_market_oi
from pytheum.api.markets_related import handle_market_related
from pytheum.api.markets_rules import handle_market_rules
from pytheum.api.markets_screen import handle_markets_screen
from pytheum.api.markets_trades import handle_market_trades
from pytheum.api.markets_whale_trades import handle_market_whale_trades
from pytheum.api.status import handle_status
from pytheum.api.traders_leaderboard import handle_traders_leaderboard
from pytheum.api.traders_profile import handle_trader_profile
from pytheum.registry import RouterRegistry, RouteSpec


def register_group_A(
    registry: RouterRegistry,
    *,
    dao: Any,
    equivalence: Any = None,
    related: Any = None,
) -> None:
    """Register all Group A data-serving routes into *registry*.

    Creates handler closures that close over ``dao``, ``equivalence``, and
    ``related`` so the Router's positional-arg dispatch convention is satisfied.
    """

    async def _status(query: dict[str, str]) -> tuple[int, dict[str, Any]]:
        return await handle_status(query, dao=dao, equivalence=equivalence, related=related)

    async def _markets_equivalents(query: dict[str, str]) -> tuple[int, dict[str, Any]]:
        return await handle_markets_equivalents(query, dao=dao, equivalence=equivalence)

    async def _markets_matched(query: dict[str, str]) -> tuple[int, dict[str, Any]]:
        return await handle_markets_matched(query, dao=dao, equivalence=equivalence)

    async def _market_equivalents(ref: str, query: dict[str, str]) -> tuple[int, dict[str, Any]]:
        return await handle_market_equivalents(ref, query, dao=dao, equivalence=equivalence)

    async def _market_rules(ref: str, query: dict[str, str]) -> tuple[int, dict[str, Any]]:
        return await handle_market_rules(ref, query, dao=dao, equivalence=equivalence)

    async def _market_related(ref: str, query: dict[str, str]) -> tuple[int, dict[str, Any]]:
        return await handle_market_related(ref, query, dao=dao, related=related)

    registry.add(RouteSpec(
        "GET", "/v1/status", _status,
        summary="Service health check and dataset summary — keyless.",
        tags=["meta"],
    ))
    registry.add(RouteSpec(
        "GET", "/v1/markets/equivalents", _markets_equivalents,
        summary="Collection of verified Kalshi<->Polymarket pairs with live quotes.",
        tags=["equivalence"],
        params={
            "limit": "Maximum number of pairs (default 50, max 200)",
            "fungible_only": "Restrict to deterministic/human-adjudicated pairs",
            "include_rules": "Include resolution rules text in each leg",
        },
    ))
    registry.add(RouteSpec(
        "GET", "/v1/markets/matched", _markets_matched,
        summary="Paginated view of all 136k+ settlement-verified cross-venue pairs.",
        tags=["equivalence"],
        params={
            "bet_type": "Filter by bet type or group alias (sports)",
            "q": "Substring filter over titles",
            "league": "Filter by league / domain",
            "date": "Filter by event date (YYYY-MM-DD)",
            "sort_by": "Sort order: volume | spread | confidence",
            "fungible_only": "Restrict to deterministic/human-adjudicated pairs",
            "min_volume": "Minimum Kalshi volume_usd",
            "limit": "Maximum number of results (default 50, max 200)",
            "offset": "Pagination offset",
        },
    ))
    registry.add(RouteSpec(
        "GET", "/v1/markets/{ref}/equivalents", _market_equivalents,
        summary="Settlement-verified counterpart market on the other venue.",
        tags=["equivalence"],
    ))
    registry.add(RouteSpec(
        "GET", "/v1/markets/{ref}/rules", _market_rules,
        summary="Full resolution rules for a market and its cross-venue equivalent.",
        tags=["equivalence"],
    ))
    registry.add(RouteSpec(
        "GET", "/v1/markets/{ref}/related", _market_related,
        summary="Correlated (non-equivalent) cross-venue markets with basis notes.",
        tags=["related"],
    ))


def register_group_B(
    registry: RouterRegistry,
    *,
    clients: Any = None,
    dao: Any = None,
) -> None:
    """Register all Group B routes into *registry*.

    Group B covers the trader-data surface (live venue quotes) and the
    /screen discovery endpoint:

    - GET /v1/markets/screen
    - GET /v1/markets/whale-trades          (registered BEFORE /{ref} catch-all)
    - GET /v1/markets/{ref}/book
    - GET /v1/markets/{ref}/trades
    - GET /v1/markets/{ref}/oi
    - GET /v1/markets/{ref}/ohlcv
    - GET /v1/markets/{ref}/holders
    - GET /v1/traders/leaderboard
    - GET /v1/traders/{wallet}

    All handler closures close over ``clients`` (TraderClients or duck-typed
    stub) and ``dao`` so positional-arg dispatch is satisfied.
    """

    async def _markets_screen(query: dict[str, str]) -> tuple[int, dict[str, Any]]:
        return await handle_markets_screen(query, dao=dao)

    # whale-trades must be registered BEFORE /{ref} catch-all routes.
    async def _whale_trades(query: dict[str, str]) -> tuple[int, dict[str, Any]]:
        return await handle_market_whale_trades(query, clients=clients)

    async def _market_book(ref: str, query: dict[str, str]) -> tuple[int, dict[str, Any]]:
        return await handle_market_book(ref, query, clients=clients)

    async def _market_trades(ref: str, query: dict[str, str]) -> tuple[int, dict[str, Any]]:
        return await handle_market_trades(ref, query, clients=clients)

    async def _market_oi(ref: str, query: dict[str, str]) -> tuple[int, dict[str, Any]]:
        return await handle_market_oi(ref, query, clients=clients)

    async def _market_ohlcv(ref: str, query: dict[str, str]) -> tuple[int, dict[str, Any]]:
        return await handle_market_ohlcv(ref, query, clients=clients, dao=dao)

    async def _market_holders(ref: str, query: dict[str, str]) -> tuple[int, dict[str, Any]]:
        return await handle_market_holders(ref, query, clients=clients)

    async def _traders_leaderboard(query: dict[str, str]) -> tuple[int, dict[str, Any]]:
        return await handle_traders_leaderboard(query, clients=clients)

    async def _trader_profile(wallet: str, query: dict[str, str]) -> tuple[int, dict[str, Any]]:
        return await handle_trader_profile(wallet, query, clients=clients)

    registry.add(RouteSpec(
        "GET", "/v1/markets/screen", _markets_screen,
        summary=(
            "Structured (non-semantic) market screen: filter by venue, status, "
            "volume, liquidity, resolution window; sort by volume|liquidity|resolution|move."
        ),
        tags=["markets"],
        params={
            "venues": "Comma-separated venue list: kalshi, polymarket",
            "status": "Market status (default: active)",
            "min_volume": "Minimum volume_usd",
            "max_volume": "Maximum volume_usd",
            "min_liquidity": "Minimum liquidity_usd",
            "resolves_before": "ISO-8601 datetime upper bound for resolution_at",
            "resolves_after": "ISO-8601 datetime lower bound for resolution_at",
            "sort_by": "volume | liquidity | resolution | move",
            "limit": "Maximum number of results (default 50, max 200)",
            "exclude_stale": "Drop expired/resolved markets (true/false)",
        },
    ))
    registry.add(RouteSpec(
        "GET", "/v1/markets/whale-trades", _whale_trades,
        summary="Large Polymarket trades with notional_usd >= min_usd. Polymarket-only.",
        tags=["trader-data"],
        params={
            "min_usd": "Minimum notional USD (default 500)",
            "limit": "Max trades (default 50, max 500)",
            "market_ref": "Optional polymarket:… ref to filter to one market",
        },
    ))
    registry.add(RouteSpec(
        "GET", "/v1/markets/{ref}/book", _market_book,
        summary="Live orderbook snapshot (coalesced ~2 s). depth 1–200, default 20.",
        tags=["trader-data"],
        params={"depth": "Order-book depth (default 20, max 200)"},
    ))
    registry.add(RouteSpec(
        "GET", "/v1/markets/{ref}/trades", _market_trades,
        summary="Recent trade tape (coalesced ~10 s). limit 1–1000, default 50.",
        tags=["trader-data"],
        params={"limit": "Max trades (default 50, max 1000)"},
    ))
    registry.add(RouteSpec(
        "GET", "/v1/markets/{ref}/oi", _market_oi,
        summary="Current open interest (coalesced ~30 s).",
        tags=["trader-data"],
    ))
    registry.add(RouteSpec(
        "GET", "/v1/markets/{ref}/ohlcv", _market_ohlcv,
        summary=(
            "OHLCV candles (venue-live; interval 1m|5m|15m|1h|1d; "
            "since/until ISO-8601 or Unix-s; limit 1–1000)."
        ),
        tags=["trader-data"],
        params={
            "interval": "Candle interval: 1m|5m|15m|1h|1d (default 1h)",
            "since": "Start of range (ISO-8601 or Unix-seconds)",
            "until": "End of range (ISO-8601 or Unix-seconds)",
            "limit": "Max candles (default 200, max 1000)",
        },
    ))
    registry.add(RouteSpec(
        "GET", "/v1/markets/{ref}/holders", _market_holders,
        summary="Holder breakdown for a Polymarket market (coalesced ~60 s). Polymarket-only.",
        tags=["trader-data"],
    ))
    registry.add(RouteSpec(
        "GET", "/v1/traders/leaderboard", _traders_leaderboard,
        summary="Polymarket trader leaderboard (coalesced 300 s). period=weekly|monthly.",
        tags=["trader-data"],
        params={"period": "Ranking period: weekly | monthly (default weekly)"},
    ))
    registry.add(RouteSpec(
        "GET", "/v1/traders/{wallet}", _trader_profile,
        summary=(
            "Polymarket trader profile — positions + activity + value "
            "(coalesced 60 s). Polymarket-only."
        ),
        tags=["trader-data"],
    ))


def register_all(
    registry: RouterRegistry,
    *,
    dao: Any,
    equivalence: Any = None,
    related: Any = None,
    clients: Any = None,
) -> None:
    """Wire all available route groups into *registry*.

    Composition order: Group A (data-serving) first, then Group B (trader-data
    + screen). ``clients`` (a TraderClients instance or duck-typed stub) is
    forwarded to Group B; pass None to skip live-quote routes.
    """
    register_group_A(registry, dao=dao, equivalence=equivalence, related=related)
    register_group_B(registry, clients=clients, dao=dao)
