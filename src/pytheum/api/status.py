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

import time
from datetime import UTC, datetime
from typing import Any

_CACHE_TTL_S = 60.0
_STALE_THRESHOLD_S = 24 * 3600  # 24 hours
_cache: tuple[float, dict[str, Any]] | None = None

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


async def handle_status(
    query: dict[str, str],  # kept for signature consistency; not used today
    *,
    dao: Any,
    equivalence: Any = None,
    related: Any = None,
) -> tuple[int, dict[str, Any]]:
    """GET /v1/status handler.

    ``equivalence`` and ``related`` are optional duck-typed index objects.
    When None the handler falls back to the module-level singletons (lazy).
    """
    global _cache

    # 60-second cache so concurrent health checks share one DAO round-trip.
    if _cache is not None and time.monotonic() - _cache[0] < _CACHE_TTL_S:
        return 200, _cache[1]

    # Lazy-load module singletons (same pattern as the other endpoints).
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

    # ---- platforms block (DAO-backed, best-effort) --------------------------
    platforms: dict[str, Any] = {}
    fetch_stats = getattr(dao, "fetch_venue_stats", None)
    if fetch_stats is not None:
        try:
            venue_rows = await fetch_stats()
            # Expected: [{"venue": str, "count": int, "last_updated": str|None}, ...]
            for row in (venue_rows or []):
                venue = str(row.get("venue") or "unknown")
                lu = row.get("last_updated")
                lu_str: str | None = None
                if lu is not None:
                    lu_str = lu.isoformat() if hasattr(lu, "isoformat") else str(lu)
                platforms[venue] = {
                    "markets": row.get("count"),
                    "last_updated": lu_str,
                    "status": "stale" if _is_stale(lu_str) else "ok",
                }
        except Exception:
            pass  # degrade gracefully — platforms stays empty

    # ---- equivalence block --------------------------------------------------
    eq_pairs = getattr(equivalence, "pairs_loaded", 0) if equivalence else 0
    eq_version = getattr(equivalence, "dataset_version", None) if equivalence else None

    # ---- related block ------------------------------------------------------
    rel_pairs = getattr(related, "pairs_loaded", 0) if related else 0

    # ---- service block ------------------------------------------------------
    body: dict[str, Any] = {
        "equivalence": {
            "pairs_loaded": eq_pairs,
            "dataset_version": eq_version,
        },
        "related": {
            "pairs_loaded": rel_pairs,
        },
        "service": {
            "version": _get_version(),
            "now": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        },
    }
    if platforms:
        body["platforms"] = platforms

    _cache = (time.monotonic(), body)
    return 200, body
