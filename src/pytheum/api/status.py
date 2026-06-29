"""GET /v1/status — keyless service health + dataset summary.

Designed for ping / load-balancer health checks AND for agents that need a
quick situational-awareness snapshot before issuing market queries.

Response shape
--------------
{
  "platforms": {
    "<venue>": {
      "markets": <int | null>,
      "last_updated": "<ISO-8601 | null>",
      "status": "ok" | "stale"
    }
  },
  "equivalence": {
    "pairs_loaded": <int>,
    "dataset_version": "<str | null>"
  },
  "related": {
    "pairs_loaded": <int>
  },
  "service": {
    "version": "<str>",
    "now": "<ISO-8601>"
  }
}

Caching
-------
The DAO query (``dao.fetch_venue_stats()``) is cached for 60 s — the same
lifetime as the equivalents-collection cache.  If the dao lacks the method,
the platforms block is omitted (graceful degradation).

Staleness threshold
-------------------
A platform is "stale" when its ``last_updated`` is older than 24 h.
"""
from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime
from typing import Any

# Background-refresh ("stale-while-revalidate") cache: /v1/status serves the cached body
# immediately and refreshes in the background, so a request NEVER blocks on the slow
# per-venue market-count (dao.fetch_venue_stats over ~459k rows ≈ 10s) — only the very
# first call after process start pays it. TTL = how often the background refresh fires.
_CACHE_TTL_S = 300.0
_STALE_THRESHOLD_S = 24 * 3600  # 24 hours
_cache: tuple[float, dict[str, Any]] | None = None
_refreshing = False

# ---- Stale guard -----------------------------------------------------------


def _is_stale(last_updated: str | None) -> bool:
    """Return True when *last_updated* is older than _STALE_THRESHOLD_S."""
    if last_updated is None:
        return False
    try:
        ts = datetime.fromisoformat(last_updated.replace("Z", "+00:00"))
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=UTC)
        age = (datetime.now(UTC) - ts).total_seconds()
        return age > _STALE_THRESHOLD_S
    except (ValueError, TypeError):
        return False


# ---- Service version (best-effort) -----------------------------------------

_service_version: str | None = None


def _get_version() -> str:
    global _service_version
    if _service_version is None:
        try:
            import importlib.metadata
            _service_version = importlib.metadata.version("pytheum")
        except Exception:
            _service_version = "dev"
    return _service_version


# ---- Handler ----------------------------------------------------------------


async def _build_status(dao: Any, equivalence: Any, related: Any) -> dict[str, Any]:
    """Build the status body. Resolves the index singletons (lazy) when not supplied.
    The platforms block runs the slow per-venue count — best-effort, degrades to empty."""
    if equivalence is None:
        try:
            from pytheum.equivalence.index import get_index
            equivalence = get_index()
        except Exception:
            equivalence = None
    if related is None:
        try:
            from pytheum.related.index import get_index as get_related
            related = get_related()
        except Exception:
            related = None

    platforms: dict[str, Any] = {}
    fetch_stats = getattr(dao, "fetch_venue_stats", None)
    if fetch_stats is not None:
        try:
            venue_rows = await fetch_stats()
            for row in (venue_rows or []):
                venue = str(row.get("venue") or "unknown")
                lu = row.get("last_updated")
                lu_str: str | None = (
                    (lu.isoformat() if hasattr(lu, "isoformat") else str(lu))
                    if lu is not None else None
                )
                platforms[venue] = {
                    "markets": row.get("count"),
                    "last_updated": lu_str,
                    "status": "stale" if _is_stale(lu_str) else "ok",
                }
        except Exception:
            pass  # degrade gracefully — platforms stays empty

    body: dict[str, Any] = {
        "equivalence": {
            "pairs_loaded": getattr(equivalence, "pairs_loaded", 0) if equivalence else 0,
            "dataset_version": getattr(equivalence, "dataset_version", None) if equivalence else None,
        },
        "related": {"pairs_loaded": getattr(related, "pairs_loaded", 0) if related else 0},
        "service": {
            "version": _get_version(),
            "now": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        },
    }
    if platforms:
        body["platforms"] = platforms
    return body


async def _refresh_cache(dao: Any, equivalence: Any, related: Any) -> None:
    """Background refresh — rebuilds the cache without blocking the request that triggered it."""
    global _cache, _refreshing
    try:
        body = await _build_status(dao, equivalence, related)
        _cache = (time.monotonic(), body)
    except Exception:
        pass  # keep the stale cache on failure
    finally:
        _refreshing = False


async def handle_status(
    query: dict[str, str],  # kept for signature consistency; not used today
    *,
    dao: Any,
    equivalence: Any = None,
    related: Any = None,
) -> tuple[int, dict[str, Any]]:
    """GET /v1/status — stale-while-revalidate.

    Fresh cache → serve it. Stale cache → serve it immediately AND kick a single
    background refresh (so the ~10s per-venue count never blocks a request). No cache
    (first call after start) → build once synchronously.
    """
    global _cache, _refreshing

    if _cache is not None:
        age = time.monotonic() - _cache[0]
        if age >= _CACHE_TTL_S and not _refreshing:
            _refreshing = True
            asyncio.create_task(_refresh_cache(dao, equivalence, related))
        return 200, _cache[1]  # serve fresh-or-stale immediately

    body = await _build_status(dao, equivalence, related)  # cold: build once
    _cache = (time.monotonic(), body)
    return 200, body
