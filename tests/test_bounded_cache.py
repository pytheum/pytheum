"""Unit tests for BoundedTTLCache (src/pytheum/api/_bounded_cache.py).

The three response caches (_screen_cache, _matched_cache, equivalents _cache)
were plain dicts that only checked TTL on read — expired entries were never
removed, so the dict grew unbounded for every distinct key (a slow memory leak
/ re-OOM risk under high-cardinality real traffic). BoundedTTLCache fixes that:
TTL enforced on read (expired -> None + removed) and writes sweep expired +
evict oldest beyond maxsize.
"""
from __future__ import annotations

from pytheum.api._bounded_cache import BoundedTTLCache


def test_set_then_get_within_ttl_returns_value() -> None:
    c = BoundedTTLCache(ttl_s=100.0, maxsize=8)
    c.set("k", {"v": 1})
    assert c.get("k") == {"v": 1}
    assert len(c) == 1


def test_get_missing_returns_none() -> None:
    c = BoundedTTLCache(ttl_s=100.0, maxsize=8)
    assert c.get("nope") is None


def test_get_after_ttl_returns_none_and_removes_entry(monkeypatch) -> None:
    import pytheum.api._bounded_cache as mod

    now = [1000.0]
    monkeypatch.setattr(mod.time, "monotonic", lambda: now[0])
    c = BoundedTTLCache(ttl_s=10.0, maxsize=8)
    c.set("k", "body")
    assert len(c) == 1
    # advance past TTL
    now[0] += 11.0
    assert c.get("k") is None
    # expired entry is purged on read, not just hidden
    assert len(c) == 0


def test_set_beyond_maxsize_evicts_oldest(monkeypatch) -> None:
    import pytheum.api._bounded_cache as mod

    # Freeze time so nothing expires; we're testing the size cap, not TTL.
    monkeypatch.setattr(mod.time, "monotonic", lambda: 500.0)
    c = BoundedTTLCache(ttl_s=1000.0, maxsize=3)
    c.set("a", 1)
    c.set("b", 2)
    c.set("c", 3)
    assert len(c) == 3
    c.set("d", 4)  # over cap -> oldest ("a") evicted
    assert len(c) == 3
    assert c.get("a") is None  # oldest gone
    assert c.get("d") == 4  # newest present
    assert c.get("b") == 2
    assert c.get("c") == 3


def test_set_sweeps_already_expired_entries(monkeypatch) -> None:
    import pytheum.api._bounded_cache as mod

    now = [0.0]
    monkeypatch.setattr(mod.time, "monotonic", lambda: now[0])
    c = BoundedTTLCache(ttl_s=10.0, maxsize=100)
    c.set("old1", 1)
    c.set("old2", 2)
    assert len(c) == 2
    # advance past TTL, then insert a fresh key — the set() should sweep the two
    # expired entries even though they were never read.
    now[0] += 11.0
    c.set("fresh", 3)
    assert len(c) == 1
    assert c.get("fresh") == 3
    assert c.get("old1") is None


def test_clear_empties_cache() -> None:
    c = BoundedTTLCache(ttl_s=100.0, maxsize=8)
    c.set("a", 1)
    c.set("b", 2)
    c.clear()
    assert len(c) == 0
    assert c.get("a") is None


def test_reinsert_refreshes_recency(monkeypatch) -> None:
    """Re-setting an existing key moves it to newest, so it survives eviction."""
    import pytheum.api._bounded_cache as mod

    monkeypatch.setattr(mod.time, "monotonic", lambda: 0.0)
    c = BoundedTTLCache(ttl_s=1000.0, maxsize=3)
    c.set("a", 1)
    c.set("b", 2)
    c.set("c", 3)
    c.set("a", 10)  # refresh "a" -> now newest; "b" is oldest
    c.set("d", 4)  # over cap -> evicts oldest ("b")
    assert c.get("b") is None
    assert c.get("a") == 10
    assert c.get("d") == 4
