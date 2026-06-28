"""Tests for the pg_trgm index DDL + the warm-up ping (pure/mocked parts)."""
import urllib.request

from scripts import warmup_ping
from scripts.ensure_search_indexes import _statements


def test_index_ddl_is_idempotent_concurrent_trigram():
    s = _statements()
    joined = "\n".join(s)
    assert "CREATE EXTENSION IF NOT EXISTS pg_trgm" in joined
    # both index builds: idempotent, non-locking, trigram
    idx = [x for x in s if x.startswith("CREATE INDEX")]
    assert len(idx) == 2
    assert all("IF NOT EXISTS" in x and "CONCURRENTLY" in x and "gin_trgm_ops" in x
               for x in idx)
    # the numeric comma-stripped path gets its own expression index
    assert any("replace(title, ',', '')" in x for x in idx)


def test_warmup_ping_ok(monkeypatch):
    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b'{"equivalence":{"pairs_loaded":142179}}'

    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: _Resp())
    ok, ms, detail = warmup_ping.ping("https://x")
    assert ok is True
    assert "142179" in detail
    assert ms >= 0


def test_warmup_ping_failure_is_reported_not_raised(monkeypatch):
    def _boom(*a, **k):
        raise OSError("connection refused")

    monkeypatch.setattr(urllib.request, "urlopen", _boom)
    ok, ms, detail = warmup_ping.ping("https://x")
    assert ok is False
    assert "connection refused" in detail
