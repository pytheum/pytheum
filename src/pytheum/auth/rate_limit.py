"""Two-layer token-bucket rate limiter.

Layer 1 — aggregate cap per token, tier-dependent.
Layer 2 — per-event-type sub-cap, prevents one event channel from
          starving others.
"""
from __future__ import annotations

import time
from typing import Literal

Tier = Literal["demo", "issued"]


class TokenBucket:
    __slots__ = ("burst", "last", "refill_per_sec", "tokens")

    def __init__(self, *, burst: float, refill_per_sec: float):
        self.burst = float(burst)
        self.refill_per_sec = float(refill_per_sec)
        self.tokens = float(burst)
        self.last = time.monotonic()

    def take(self, n: float = 1.0) -> bool:
        now = time.monotonic()
        elapsed = now - self.last
        self.last = now
        self.tokens = min(self.burst, self.tokens + elapsed * self.refill_per_sec)
        if self.tokens >= n:
            self.tokens -= n
            return True
        return False


# Aggregate caps per tier — spec §6.
_TIER: dict[str, tuple[int, int]] = {
    "demo":   (500,  50),
    "issued": (2000, 200),
}

# Per-event sub-caps — spec §6.
_PER_EVENT: dict[str, int] = {
    "tick_price":    100,
    "tick_book":      50,
    "news_headline":  30,
    "social_post":    50,
    "hn_story":        5,
    "macro_release":  10,
}


class RateLimiter:
    def __init__(self, *, aggregate: TokenBucket, per_event: dict[str, TokenBucket]):
        self.aggregate = aggregate
        self.per_event = per_event

    @classmethod
    def for_tier(cls, tier: Tier) -> RateLimiter:
        burst, refill = _TIER[tier]
        agg = TokenBucket(burst=burst, refill_per_sec=refill)
        per = {
            evt: TokenBucket(burst=cap, refill_per_sec=cap)
            for evt, cap in _PER_EVENT.items()
        }
        return cls(aggregate=agg, per_event=per)

    def allow(self, event_type: str) -> bool:
        # Sub-cap first — cheaper to reject early.
        sub = self.per_event.get(event_type)
        if sub is not None and not sub.take():
            return False
        return self.aggregate.take()
