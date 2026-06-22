"""GET /v1/metrics — keyless service metrics for the upstream venue layer.

Exposes the process-global venue-call counters
(:mod:`pytheum.trader.metrics`) so request coalescing is **measurable**, not
just inferred from the load test.  Per venue (kalshi / polymarket) it reports:

* ``requests``       — calls into the coalescing cache tagged to that venue
* ``hits``           — served from the TTL cache (no upstream round-trip)
* ``coalesced``      — joined an in-flight call (no upstream round-trip)
* ``upstream_calls`` — fresh HTTP round-trips actually made to the venue
* ``errors``         — upstream calls that raised

plus a ``totals`` block with ``coalesce_savings`` (hits + coalesced) and
``coalesce_ratio`` (savings / requests).

Keyless by design — same posture as ``/v1/status``: it carries no secrets and
no per-market data, only aggregate process counters, and is useful to load
balancers / dashboards that should not need an API key.  It is also exempt
from the API-key gate's allowlist consideration in that it never reveals
client identities.
"""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from pytheum.trader.metrics import get_metrics

__all__ = ["handle_metrics"]


async def handle_metrics(query: dict[str, str]) -> tuple[int, dict[str, Any]]:
    """GET /v1/metrics handler — returns the venue-call counter snapshot."""
    snap = get_metrics().snapshot()
    snap["now"] = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    return 200, snap
