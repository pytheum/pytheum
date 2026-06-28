"""warmup_ping target composition — must include the search path (the cold-buffer fix), not just status."""
from __future__ import annotations

import importlib

import scripts.warmup_ping as wp


def test_targets_include_status_and_search(monkeypatch) -> None:
    monkeypatch.delenv("PYTHEUM_WARMUP_QUERIES", raising=False)
    t = wp.warmup_targets()
    assert t[0] == "/v1/status"                                  # liveness first
    search = [p for p in t if p.startswith("/v1/markets/search")]
    assert search, "warmup MUST hit the search path — /v1/status alone doesn't warm the buffers"
    assert all("limit=50" in p for p in search)


def test_queries_env_override_and_urlencode(monkeypatch) -> None:
    monkeypatch.setenv("PYTHEUM_WARMUP_QUERIES", "fed rate, , bitcoin")
    importlib.reload(wp)
    t = wp.warmup_targets()
    # blanks dropped, spaces percent-encoded
    assert any("q=fed%20rate" in p for p in t)
    assert any("q=bitcoin" in p for p in t)
    assert sum(p.startswith("/v1/markets/search") for p in t) == 2
    monkeypatch.delenv("PYTHEUM_WARMUP_QUERIES", raising=False)
    importlib.reload(wp)


def test_ping_never_raises_on_bad_host() -> None:
    ok, ms, detail = wp.ping("http://127.0.0.1:1", "/v1/status", timeout=0.2)
    assert ok is False and ms >= 0 and isinstance(detail, str)
