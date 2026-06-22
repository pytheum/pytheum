"""Process-global upstream venue-call counters.

The trader-data handlers fetch live quotes through
:class:`~pytheum.trader.cache.SingleFlightCache`, which coalesces concurrent
requests for the same key into ONE underlying venue call.  Coalescing was
previously only *inferred* from the load test (248 RPS, far fewer upstream
calls).  This module makes it *measurable*: every call into the cache records,
per venue, whether it hit the TTL cache, joined an in-flight call (coalesced),
or fired a fresh upstream call.

The relation between the counters is::

    requests == hits + coalesced + upstream_calls + errors

so ``upstream_calls`` is the real number of HTTP round-trips made to a venue,
and ``hits + coalesced`` is everything coalescing/caching saved.

State is a module-level singleton.  Like the MCP rate limiter, it is
per-process — the service runs single-process so this is exact.  Counters are
monotonic since process start (or the last :func:`reset`, used by tests).

Thread-safety: asyncio single-threaded only; the cache mutates these from
within a single event loop, so plain ``+=`` is race-free between awaits.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

__all__ = ["VenueMetrics", "get_metrics", "reset"]


@dataclass
class _Counters:
    """Per-venue counters.  All monotonic since process start."""

    requests: int = 0       # total get_or_fetch calls tagged to this venue
    hits: int = 0           # served from the TTL cache (no upstream call)
    coalesced: int = 0      # joined an in-flight call (no upstream call)
    upstream_calls: int = 0  # fresh upstream HTTP round-trips actually fired
    errors: int = 0         # upstream calls that raised


@dataclass
class VenueMetrics:
    """Coalescing + upstream-call counters, keyed by venue.

    ``venues`` always carries the two real venues so the metrics surface is
    stable even before any traffic; an unrecognised tag is bucketed under
    ``"other"`` (created lazily).
    """

    venues: dict[str, _Counters] = field(
        default_factory=lambda: {"kalshi": _Counters(), "polymarket": _Counters()}
    )

    def _bucket(self, venue: str) -> _Counters:
        key = venue if venue in ("kalshi", "polymarket") else "other"
        c = self.venues.get(key)
        if c is None:
            c = _Counters()
            self.venues[key] = c
        return c

    # -- recording (called by SingleFlightCache) --------------------------- #

    def record_request(self, venue: str) -> None:
        self._bucket(venue).requests += 1

    def record_hit(self, venue: str) -> None:
        self._bucket(venue).hits += 1

    def record_coalesced(self, venue: str) -> None:
        self._bucket(venue).coalesced += 1

    def record_upstream_call(self, venue: str) -> None:
        self._bucket(venue).upstream_calls += 1

    def record_error(self, venue: str) -> None:
        self._bucket(venue).errors += 1

    # -- export ------------------------------------------------------------ #

    def snapshot(self) -> dict[str, Any]:
        """Return a JSON-serialisable snapshot with per-venue counters + totals.

        ``coalesce_savings`` is ``hits + coalesced`` — the requests that did NOT
        cost an upstream round-trip — and ``coalesce_ratio`` expresses that as a
        fraction of total requests (0.0 when there has been no traffic).
        """
        per_venue: dict[str, Any] = {}
        totals = _Counters()
        for name, c in self.venues.items():
            per_venue[name] = asdict(c)
            totals.requests += c.requests
            totals.hits += c.hits
            totals.coalesced += c.coalesced
            totals.upstream_calls += c.upstream_calls
            totals.errors += c.errors
        savings = totals.hits + totals.coalesced
        ratio = (savings / totals.requests) if totals.requests else 0.0
        totals_d = asdict(totals)
        totals_d["coalesce_savings"] = savings
        totals_d["coalesce_ratio"] = round(ratio, 4)
        return {"venues": per_venue, "totals": totals_d}

    def reset(self) -> None:
        self.venues = {"kalshi": _Counters(), "polymarket": _Counters()}


# Module-level singleton — shared across all handlers + the cache.
_metrics = VenueMetrics()


def get_metrics() -> VenueMetrics:
    """Return the process-global :class:`VenueMetrics` singleton."""
    return _metrics


def reset() -> None:
    """Reset the global counters (used by tests)."""
    _metrics.reset()
