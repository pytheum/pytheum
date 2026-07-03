"""The pytheum SDK clients — :class:`AsyncClient` (primary) and sync :class:`Client`.

Both wrap the same resilient transport (pooling + concurrency governor + retries)
and expose every REST route as a typed method. The REST API returns direct JSON
payloads, so methods return parsed dict/list bodies (optionally wrapped in the
:mod:`~pytheum.client.models` dataclasses via ``model=...``).

    import asyncio
    from pytheum.client import AsyncClient

    async def main():
        async with AsyncClient() as px:
            print((await px.status())["equivalence"]["pairs_loaded"])
            pairs = await px.matched_pairs(sort_by="net_edge", limit=20)
            # fan-out, safely bounded by the governor:
            books = await px.gather(*(px.orderbook(p["kalshi"]["id"]) for p in pairs["pairs"]))

    asyncio.run(main())
"""
from __future__ import annotations

import asyncio
from collections.abc import Awaitable
from typing import Any
from urllib.parse import quote

from ._registry import BY_NAME, Endpoint
from ._transport import (
    DEFAULT_BASE_URL,
    AsyncTransport,
    RequestSpec,
    RetryConfig,
    SyncTransport,
    build_limits,
    build_timeout,
)

_USER_AGENT = "pytheum-python/0.1"


class _ClientBase:
    """Shared config + RequestSpec construction for both clients."""

    def __init__(
        self,
        *,
        base_url: str = DEFAULT_BASE_URL,
        api_key: str | None = None,
        max_concurrency: int = 8,
        max_retries: int = 3,
        connect_timeout: float = 5.0,
        read_timeout: float = 30.0,
        write_timeout: float = 10.0,
        pool_timeout: float = 5.0,
        max_connections: int = 20,
        max_keepalive: int = 10,
        http2: bool = False,
    ) -> None:
        headers = {"User-Agent": _USER_AGENT, "Accept": "application/json"}
        if api_key:  # edge is keyless today; forward a key if the account ever needs one
            headers["Authorization"] = f"Bearer {api_key}"
        self._cfg = dict(
            base_url=base_url.rstrip("/"),
            headers=headers,
            limits=build_limits(max_connections, max_keepalive),
            timeout=build_timeout(connect_timeout, read_timeout, write_timeout, pool_timeout),
            max_concurrency=max_concurrency,
            retry=RetryConfig(max_retries=max_retries),
            http2=http2,
        )

    @staticmethod
    def _spec(name: str, *, ref: str | None = None, wallet: str | None = None,
              event_id: str | None = None, **params: Any) -> RequestSpec:
        ep: Endpoint = BY_NAME[name]
        path = ep.path
        if "{ref}" in path:
            if ref is None:
                raise ValueError(f"{name}() requires a market ref (venue:id)")
            path = path.replace("{ref}", quote(ref, safe=""))
        if "{wallet}" in path:
            if wallet is None:
                raise ValueError(f"{name}() requires a wallet")
            path = path.replace("{wallet}", quote(wallet, safe=""))
        if "{event_id}" in path:
            if event_id is None:
                raise ValueError(f"{name}() requires an event_id")
            path = path.replace("{event_id}", quote(event_id, safe=""))
        # only forward params this endpoint declares (silently ignore Nones later)
        clean = {k: v for k, v in params.items() if k in ep.params}
        return RequestSpec(ep.method, path, clean)


class AsyncClient(_ClientBase):
    """Async pytheum client. Use as an async context manager (single pooled client)."""

    def __init__(self, **kw: Any) -> None:
        super().__init__(**kw)
        self._t = AsyncTransport(**self._cfg)

    async def __aenter__(self) -> AsyncClient:
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._t.aclose()

    async def _call(self, name: str, **kw: Any) -> Any:
        return await self._t.request(self._spec(name, **kw))

    async def gather(self, *aws: Awaitable[Any], return_exceptions: bool = False) -> list[Any]:
        """Await many client calls concurrently; the governor bounds real in-flight load."""
        return await asyncio.gather(*aws, return_exceptions=return_exceptions)

    # -- meta / health --
    async def status(self) -> Any: return await self._call("status")
    async def quality(self) -> Any: return await self._call("quality")
    async def about(self) -> Any: return await self._call("about")
    async def guide(self) -> Any: return await self._call("guide")

    # -- discovery --
    async def search(self, q: str, *, limit: int | None = None, venue: Any = None,
                     min_similarity: float | None = None, group_by: str | None = None,
                     exclude_stale: bool | None = None) -> Any:
        return await self._call("search", q=q, limit=limit, venue=_venue(venue),
                                min_similarity=min_similarity, group_by=group_by,
                                exclude_stale=exclude_stale)

    async def screen(self, *, venue: Any = None, status: str | None = None,
                     min_volume: float | None = None, max_volume: float | None = None,
                     sort_by: str | None = None, limit: int | None = None,
                     resolves_before: str | None = None, resolves_after: str | None = None) -> Any:
        return await self._call("screen", venue=_venue(venue), status=status,
                                min_volume=min_volume, max_volume=max_volume, sort_by=sort_by,
                                limit=limit, resolves_before=resolves_before,
                                resolves_after=resolves_after)

    async def get_market(self, ref: str) -> Any: return await self._call("get_market", ref=ref)

    # -- cross-venue graph --
    async def equivalents(self, ref: str, *, limit: int | None = None,
                          include_rules: bool | None = None) -> Any:
        return await self._call("equivalents", ref=ref, limit=limit, include_rules=include_rules)

    async def matched_pairs(self, *, bet_type: str | None = None, q: str | None = None,
                            min_volume: float | None = None, sort_by: str | None = None,
                            limit: int | None = None, offset: int | None = None,
                            league: str | None = None, date: str | None = None,
                            fungible_only: bool | None = None) -> Any:
        return await self._call("matched_pairs", bet_type=bet_type, q=q, min_volume=min_volume,
                                sort_by=sort_by, limit=limit, offset=offset, league=league,
                                date=date, fungible_only=fungible_only)

    async def find_divergences(self, *, limit: int | None = None,
                               min_volume: float | None = None) -> Any:
        """The arb radar — matched pairs ranked by executable, fee-netted net_edge."""
        return await self.matched_pairs(sort_by="net_edge", limit=limit, min_volume=min_volume)

    async def related(self, ref: str, *, limit: int | None = None) -> Any:
        return await self._call("related", ref=ref, limit=limit)

    async def rules(self, ref: str) -> Any: return await self._call("rules", ref=ref)

    # -- context --
    async def context(self, ref: str, *, limit: int | None = None) -> Any:
        return await self._call("context", ref=ref, limit=limit)

    async def bundle_context(self, ref: str, *, limit: int | None = None) -> Any:
        return await self._call("bundle_context", ref=ref, limit=limit)

    async def context_batch(self, refs: list[str]) -> Any:
        return await self._call("context_batch", refs=",".join(refs))

    async def event_related_markets(self, event_id: str, *, limit: int | None = None) -> Any:
        return await self._call("event_related_markets", event_id=event_id, limit=limit)

    # -- market data --
    async def orderbook(self, ref: str) -> Any: return await self._call("orderbook", ref=ref)
    async def trades(self, ref: str) -> Any: return await self._call("trades", ref=ref)
    async def ohlcv(self, ref: str, *, interval: str | None = None,
                    limit: int | None = None) -> Any:
        return await self._call("ohlcv", ref=ref, interval=interval, limit=limit)
    async def open_interest(self, ref: str) -> Any: return await self._call("open_interest", ref=ref)
    async def history(self, ref: str, *, full: bool | None = None) -> Any:
        return await self._call("history", ref=ref, full=full)
    async def flow(self, ref: str) -> Any: return await self._call("flow", ref=ref)

    # -- trader intel --
    async def leaderboard(self, *, period: str | None = None) -> Any:
        return await self._call("leaderboard", period=period)
    async def trader(self, wallet: str) -> Any: return await self._call("trader", wallet=wallet)
    async def holders(self, ref: str) -> Any: return await self._call("holders", ref=ref)
    async def whale_trades(self, *, min_usd: float | None = None,
                           limit: int | None = None) -> Any:
        return await self._call("whale_trades", min_usd=min_usd, limit=limit)

    # -- batch fan-out helpers (throughput) --
    async def get_markets(self, refs: list[str]) -> list[Any]:
        """Hydrate many market cores concurrently (governor-bounded)."""
        return await self.gather(*(self.get_market(r) for r in refs))

    async def equivalents_many(self, refs: list[str], *, limit: int | None = None) -> list[Any]:
        return await self.gather(*(self.equivalents(r, limit=limit) for r in refs))


class Client(_ClientBase):
    """Synchronous pytheum client — a real httpx.Client (not asyncio-wrapped)."""

    def __init__(self, **kw: Any) -> None:
        super().__init__(**kw)
        self._t = SyncTransport(**self._cfg)

    def __enter__(self) -> Client:
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def close(self) -> None:
        self._t.close()

    def _call(self, name: str, **kw: Any) -> Any:
        return self._t.request(self._spec(name, **kw))

    def status(self) -> Any: return self._call("status")
    def quality(self) -> Any: return self._call("quality")
    def about(self) -> Any: return self._call("about")
    def guide(self) -> Any: return self._call("guide")

    def search(self, q: str, *, limit: int | None = None, venue: Any = None,
               min_similarity: float | None = None, group_by: str | None = None,
               exclude_stale: bool | None = None) -> Any:
        return self._call("search", q=q, limit=limit, venue=_venue(venue),
                          min_similarity=min_similarity, group_by=group_by,
                          exclude_stale=exclude_stale)

    def screen(self, *, venue: Any = None, status: str | None = None,
               min_volume: float | None = None, max_volume: float | None = None,
               sort_by: str | None = None, limit: int | None = None,
               resolves_before: str | None = None, resolves_after: str | None = None) -> Any:
        return self._call("screen", venue=_venue(venue), status=status, min_volume=min_volume,
                          max_volume=max_volume, sort_by=sort_by, limit=limit,
                          resolves_before=resolves_before, resolves_after=resolves_after)

    def get_market(self, ref: str) -> Any: return self._call("get_market", ref=ref)

    def equivalents(self, ref: str, *, limit: int | None = None,
                    include_rules: bool | None = None) -> Any:
        return self._call("equivalents", ref=ref, limit=limit, include_rules=include_rules)

    def matched_pairs(self, *, bet_type: str | None = None, q: str | None = None,
                      min_volume: float | None = None, sort_by: str | None = None,
                      limit: int | None = None, offset: int | None = None,
                      league: str | None = None, date: str | None = None,
                      fungible_only: bool | None = None) -> Any:
        return self._call("matched_pairs", bet_type=bet_type, q=q, min_volume=min_volume,
                          sort_by=sort_by, limit=limit, offset=offset, league=league,
                          date=date, fungible_only=fungible_only)

    def find_divergences(self, *, limit: int | None = None,
                         min_volume: float | None = None) -> Any:
        return self.matched_pairs(sort_by="net_edge", limit=limit, min_volume=min_volume)

    def related(self, ref: str, *, limit: int | None = None) -> Any:
        return self._call("related", ref=ref, limit=limit)

    def rules(self, ref: str) -> Any: return self._call("rules", ref=ref)

    def context(self, ref: str, *, limit: int | None = None) -> Any:
        return self._call("context", ref=ref, limit=limit)

    def bundle_context(self, ref: str, *, limit: int | None = None) -> Any:
        return self._call("bundle_context", ref=ref, limit=limit)

    def context_batch(self, refs: list[str]) -> Any:
        return self._call("context_batch", refs=",".join(refs))

    def event_related_markets(self, event_id: str, *, limit: int | None = None) -> Any:
        return self._call("event_related_markets", event_id=event_id, limit=limit)

    def orderbook(self, ref: str) -> Any: return self._call("orderbook", ref=ref)
    def trades(self, ref: str) -> Any: return self._call("trades", ref=ref)
    def ohlcv(self, ref: str, *, interval: str | None = None, limit: int | None = None) -> Any:
        return self._call("ohlcv", ref=ref, interval=interval, limit=limit)
    def open_interest(self, ref: str) -> Any: return self._call("open_interest", ref=ref)
    def history(self, ref: str, *, full: bool | None = None) -> Any:
        return self._call("history", ref=ref, full=full)
    def flow(self, ref: str) -> Any: return self._call("flow", ref=ref)

    def leaderboard(self, *, period: str | None = None) -> Any:
        return self._call("leaderboard", period=period)
    def trader(self, wallet: str) -> Any: return self._call("trader", wallet=wallet)
    def holders(self, ref: str) -> Any: return self._call("holders", ref=ref)
    def whale_trades(self, *, min_usd: float | None = None, limit: int | None = None) -> Any:
        return self._call("whale_trades", min_usd=min_usd, limit=limit)


def _venue(v: Any) -> Any:
    """Accept a str, comma-list, or sequence for the venue param → csv or None."""
    if v is None:
        return None
    if isinstance(v, str):
        return v
    return ",".join(str(x) for x in v)
