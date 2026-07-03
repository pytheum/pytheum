"""Declarative registry of every REST endpoint the client exposes.

Single source of truth: the typed client methods build ``RequestSpec``s from
these entries, and the test suite asserts method↔registry completeness so a new
route can't silently go unwrapped.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Endpoint:
    name: str                       # client method name
    method: str                     # HTTP verb
    path: str                       # may contain {ref} / {wallet} / {id} / {event_id}
    params: tuple[str, ...] = ()     # accepted query params
    pit: bool = False               # served by the PIT overlay
    note: str = ""


# ref-encoded path params (url-encoded by the method layer)
REGISTRY: tuple[Endpoint, ...] = (
    Endpoint("status", "GET", "/v1/status"),
    Endpoint("quality", "GET", "/v1/quality"),
    Endpoint("about", "GET", "/v1/about"),
    Endpoint("guide", "GET", "/v1/guide"),
    Endpoint("search", "GET", "/v1/markets/search",
             ("q", "limit", "venue", "min_similarity", "group_by", "exclude_stale")),
    Endpoint("screen", "GET", "/v1/markets/screen",
             ("venue", "status", "min_volume", "max_volume", "sort_by", "limit",
              "resolves_before", "resolves_after")),
    Endpoint("get_market", "GET", "/v1/markets/{ref}/core"),
    Endpoint("equivalents", "GET", "/v1/markets/{ref}/equivalents",
             ("limit", "include_rules")),
    Endpoint("matched_pairs", "GET", "/v1/markets/matched",
             ("bet_type", "q", "min_volume", "sort_by", "limit", "offset",
              "league", "date", "fungible_only")),
    Endpoint("related", "GET", "/v1/markets/{ref}/related", ("limit",)),
    Endpoint("rules", "GET", "/v1/markets/{ref}/rules"),
    Endpoint("context", "GET", "/v1/markets/{ref}/context", ("limit",), pit=True),
    Endpoint("bundle_context", "GET", "/v1/bundles/{ref}/context", ("limit",), pit=True),
    Endpoint("context_batch", "GET", "/v1/markets/context-batch", ("refs",), pit=True),
    Endpoint("event_related_markets", "GET", "/v1/events/{event_id}/related-markets",
             ("limit",), pit=True),
    Endpoint("orderbook", "GET", "/v1/markets/{ref}/book"),
    Endpoint("trades", "GET", "/v1/markets/{ref}/trades"),
    Endpoint("ohlcv", "GET", "/v1/markets/{ref}/ohlcv", ("interval", "limit"), pit=True),
    Endpoint("open_interest", "GET", "/v1/markets/{ref}/oi"),
    Endpoint("history", "GET", "/v1/markets/{ref}/history", ("full",), pit=True),
    Endpoint("flow", "GET", "/v1/markets/{ref}/flow", (), pit=True),
    Endpoint("leaderboard", "GET", "/v1/traders/leaderboard", ("period",)),
    Endpoint("trader", "GET", "/v1/traders/{wallet}"),
    Endpoint("holders", "GET", "/v1/markets/{ref}/holders"),
    Endpoint("whale_trades", "GET", "/v1/markets/whale-trades", ("min_usd", "limit")),
)

BY_NAME: dict[str, Endpoint] = {e.name: e for e in REGISTRY}
