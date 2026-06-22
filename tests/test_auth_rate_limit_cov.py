"""Coverage tests for pytheum.auth.rate_limit — TokenBucket + RateLimiter."""
from __future__ import annotations

from collections.abc import Iterator

import pytest

import pytheum.auth.rate_limit as rl
from pytheum.auth.rate_limit import (
    _PER_EVENT,
    _TIER,
    RateLimiter,
    TokenBucket,
)


@pytest.fixture
def fake_clock(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[float]]:
    """Patch time.monotonic in the module under test to a controllable value."""
    now = [1000.0]
    monkeypatch.setattr(rl.time, "monotonic", lambda: now[0])
    yield now


# ---------------------------------------------------------------------------
# TokenBucket
# ---------------------------------------------------------------------------


def test_bucket_init_full(fake_clock: list[float]) -> None:
    b = TokenBucket(burst=5, refill_per_sec=1)
    assert b.tokens == 5.0
    assert b.burst == 5.0
    assert b.refill_per_sec == 1.0


def test_bucket_take_consumes_until_empty(fake_clock: list[float]) -> None:
    b = TokenBucket(burst=3, refill_per_sec=0)
    assert b.take() is True
    assert b.take() is True
    assert b.take() is True
    # No refill (rate 0, clock frozen) → denied
    assert b.take() is False


def test_bucket_take_n_greater_than_one(fake_clock: list[float]) -> None:
    b = TokenBucket(burst=10, refill_per_sec=0)
    assert b.take(4) is True
    assert b.tokens == pytest.approx(6.0)
    assert b.take(7) is False  # only 6 left
    assert b.take(6) is True


def test_bucket_refills_over_time(fake_clock: list[float]) -> None:
    b = TokenBucket(burst=10, refill_per_sec=2)
    assert b.take(10) is True
    assert b.take() is False
    # Advance 3 seconds → +6 tokens
    fake_clock[0] += 3.0
    assert b.take() is True  # consumes 1 of 6
    assert b.take(5) is True


def test_bucket_refill_capped_at_burst(fake_clock: list[float]) -> None:
    b = TokenBucket(burst=4, refill_per_sec=100)
    assert b.take(4) is True
    # Advance a long time → would be +1000, capped at burst (4)
    fake_clock[0] += 10.0
    assert b.take(4) is True
    assert b.take() is False  # not 1004 tokens, just 4


# ---------------------------------------------------------------------------
# RateLimiter.for_tier
# ---------------------------------------------------------------------------


def test_for_tier_demo_caps() -> None:
    lim = RateLimiter.for_tier("demo")
    burst, refill = _TIER["demo"]
    assert lim.aggregate.burst == float(burst)
    assert lim.aggregate.refill_per_sec == float(refill)
    assert set(lim.per_event) == set(_PER_EVENT)


def test_for_tier_issued_caps() -> None:
    lim = RateLimiter.for_tier("issued")
    burst, refill = _TIER["issued"]
    assert lim.aggregate.burst == float(burst)
    # Per-event buckets initialized to their cap as both burst and refill
    for evt, cap in _PER_EVENT.items():
        assert lim.per_event[evt].burst == float(cap)
        assert lim.per_event[evt].refill_per_sec == float(cap)


# ---------------------------------------------------------------------------
# RateLimiter.allow
# ---------------------------------------------------------------------------


def test_allow_unknown_event_only_hits_aggregate(fake_clock: list[float]) -> None:
    agg = TokenBucket(burst=2, refill_per_sec=0)
    lim = RateLimiter(aggregate=agg, per_event={})
    assert lim.allow("mystery_event") is True
    assert lim.allow("mystery_event") is True
    assert lim.allow("mystery_event") is False  # aggregate exhausted


def test_allow_sub_cap_rejects_before_aggregate(fake_clock: list[float]) -> None:
    agg = TokenBucket(burst=100, refill_per_sec=0)
    sub = TokenBucket(burst=1, refill_per_sec=0)
    lim = RateLimiter(aggregate=agg, per_event={"tick_price": sub})
    assert lim.allow("tick_price") is True
    # Sub-cap exhausted → rejected, aggregate untouched
    assert lim.allow("tick_price") is False
    assert agg.tokens == pytest.approx(99.0)  # only the first call hit aggregate


def test_allow_aggregate_rejects_even_when_sub_ok(fake_clock: list[float]) -> None:
    agg = TokenBucket(burst=1, refill_per_sec=0)
    sub = TokenBucket(burst=100, refill_per_sec=0)
    lim = RateLimiter(aggregate=agg, per_event={"tick_price": sub})
    assert lim.allow("tick_price") is True
    assert lim.allow("tick_price") is False  # aggregate empty, sub still has room
    assert sub.tokens == pytest.approx(98.0)  # sub decremented on both attempts


def test_allow_for_tier_integration(fake_clock: list[float]) -> None:
    lim = RateLimiter.for_tier("demo")
    # hn_story sub-cap is 5 — sixth should fail on the sub-cap
    for _ in range(5):
        assert lim.allow("hn_story") is True
    assert lim.allow("hn_story") is False
