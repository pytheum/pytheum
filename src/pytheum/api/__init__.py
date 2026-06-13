"""Data-serving API route registration (Group A).

``register_all(registry, *, dao, ...)``
    Registers Group A routes: status, equivalents (collection + per-ref),
    matched, rules, related.  Intended to be the first call in the wiring
    sequence; Group B (trader/venue_stream/mcp) appends after.

Handler closures capture ``dao``, ``equivalence``, and ``related`` at
registration time so the Router can call them as positional-only callables
(``handler(*path_args, query)``).

Boundary: this module must NOT import from pytheum_pit (enforced by
.importlinter). markets_screen is excluded — BOUNDARY VIOLATION: imports
pit_helpers.build_outcome_ladder which depends on IndexHit; report only, do
not move that handler.
"""
from __future__ import annotations

from typing import Any

from pytheum.api.markets_equivalents import handle_market_equivalents, handle_markets_equivalents
from pytheum.api.markets_matched import handle_markets_matched
from pytheum.api.markets_related import handle_market_related
from pytheum.api.markets_rules import handle_market_rules
from pytheum.api.status import handle_status
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


def register_all(
    registry: RouterRegistry,
    *,
    dao: Any,
    equivalence: Any = None,
    related: Any = None,
) -> None:
    """Wire all available route groups into *registry*.

    Composition order: Group A (data-serving) first. Group B (trader,
    venue_stream, mcp) appends after — callers should extend by importing
    and calling their own register function after this one.
    """
    register_group_A(registry, dao=dao, equivalence=equivalence, related=related)
