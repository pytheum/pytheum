"""Bounded TTL cache for the API response caches.

The /v1/markets/{screen,matched,equivalents} response caches were plain
module-level dicts that checked TTL on READ only — expired entries were never
removed, so each dict grew unbounded for every distinct cache key (per-param
combos / per-ref). Under diverse real traffic that is a slow memory leak /
re-OOM risk. BoundedTTLCache bounds memory two ways: TTL is enforced on read
(expired entries return None and are purged), and every write sweeps expired
entries then evicts the oldest beyond ``maxsize``.

Dependency-free (stdlib OrderedDict + time). Single-process / single-event-loop
use only — like the dicts it replaces, it is not thread-safe and needs no lock
under the server's cooperative async model (no ``await`` inside get/set).
"""
from __future__ import annotations

import time
from collections import OrderedDict
from typing import Any


class BoundedTTLCache:
    """Insertion-ordered TTL cache with a hard size cap.

    - ``get(key)`` returns the stored value, or ``None`` if missing/expired;
      an expired entry is removed on read.
    - ``set(key, value)`` sweeps expired entries, inserts/refreshes the key as
      newest, then evicts the oldest while over ``maxsize``.
    """

    def __init__(self, *, ttl_s: float, maxsize: int) -> None:
        self._ttl = ttl_s
        self._max = maxsize
        self._d: OrderedDict[str, tuple[float, Any]] = OrderedDict()

    def get(self, key: str) -> Any | None:
        hit = self._d.get(key)
        if hit is None:
            return None
        if time.monotonic() - hit[0] >= self._ttl:
            self._d.pop(key, None)
            return None
        return hit[1]

    def set(self, key: str, value: Any) -> None:
        now = time.monotonic()
        # Sweep expired entries (cheap; bounded by current size) so a churn of
        # high-cardinality keys can't accumulate stale entries between reads.
        expired = [k for k, (ts, _) in self._d.items() if now - ts >= self._ttl]
        for k in expired:
            self._d.pop(k, None)
        self._d[key] = (now, value)
        self._d.move_to_end(key)  # refresh recency on re-set
        while len(self._d) > self._max:
            self._d.popitem(last=False)  # evict oldest (FIFO insertion order)

    def clear(self) -> None:
        self._d.clear()

    def __len__(self) -> int:
        return len(self._d)
