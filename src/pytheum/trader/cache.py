"""TTL + LRU cache with asyncio single-flight coalescing.

Design:
  - Per-key (endpoint, ref, params) in-flight map:  concurrent requests for the
    same key await ONE underlying venue call.  The Future is stored before the
    first await so a second caller landing while the first is in-flight joins it
    immediately (no lock needed — asyncio is single-threaded between awaits).
  - After the fetch completes the result is cached for `ttl` seconds.
  - On error: the exception is propagated to ALL waiters; the in-flight entry is
    cleared so the next call retries (per spec "exceptions propagate … then clear").
  - LRU cap: oldest keys are evicted when the cache exceeds `_LRU_MAX` entries.

TTL constants:
    _TTL_BOOK   = 2s   (orderbook)
    _TTL_TRADES = 10s  (recent tape)
    _TTL_OI     = 30s  (open interest)
"""
from __future__ import annotations

import asyncio
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

__all__ = ["SingleFlightCache", "_TTL_BOOK", "_TTL_TRADES", "_TTL_OI"]

_TTL_BOOK: float = 2.0
_TTL_TRADES: float = 10.0
_TTL_OI: float = 30.0
_LRU_MAX: int = 2048

T = TypeVar("T")


class SingleFlightCache:
    """Per-key coalescing cache.

    Thread-safety: asyncio single-threaded only.  Do not share across threads.
    """

    def __init__(self, max_size: int = _LRU_MAX) -> None:
        self._max_size = max_size
        # key -> (result, expires_monotonic)
        self._cache: OrderedDict[Any, tuple[Any, float]] = OrderedDict()
        # key -> Future in-flight
        self._in_flight: dict[Any, asyncio.Future[Any]] = {}

    async def get_or_fetch(
        self,
        key: Any,
        ttl: float,
        make_coro: Callable[[], Awaitable[T]],
    ) -> T:
        """Return cached result or fetch exactly once per key at a time.

        `make_coro` is called at most once per cache-miss window (the factory is
        only invoked by the *first* caller; concurrent callers join its Future).
        """
        loop = asyncio.get_running_loop()
        now = loop.time()

        # ── 1. Cache hit ────────────────────────────────────────────────────
        entry = self._cache.get(key)
        if entry is not None and now < entry[1]:
            self._cache.move_to_end(key)
            return entry[0]  # type: ignore[no-any-return]

        # ── 2. Already in-flight — join it (no yield before this check, so
        #       no race condition with step 3 below) ─────────────────────────
        existing = self._in_flight.get(key)
        if existing is not None:
            return await existing  # type: ignore[no-any-return]

        # ── 3. We are the initiator — register Future BEFORE any yield ──────
        fut: asyncio.Future[T] = loop.create_future()
        self._in_flight[key] = fut

        try:
            result = await make_coro()
            # Cache the result
            self._cache[key] = (result, loop.time() + ttl)
            self._cache.move_to_end(key)
            if len(self._cache) > self._max_size:
                self._cache.popitem(last=False)
            if not fut.done():
                fut.set_result(result)
        except BaseException as exc:
            # Propagate exception to all waiters, then clear so next call retries.
            if not fut.done():
                fut.set_exception(exc)
            raise
        finally:
            # Always clean up so future retries see a fresh in-flight slot.
            self._in_flight.pop(key, None)

        return result
