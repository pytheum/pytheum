"""Test the pg_trgm substring-search index DDL is idempotent, non-locking, trigram."""
from scripts.ensure_search_indexes import _statements


def test_index_ddl_is_idempotent_concurrent_trigram():
    s = _statements()
    joined = "\n".join(s)
    assert "CREATE EXTENSION IF NOT EXISTS pg_trgm" in joined
    idx = [x for x in s if x.startswith("CREATE INDEX")]
    assert len(idx) == 2
    assert all("IF NOT EXISTS" in x and "CONCURRENTLY" in x and "gin_trgm_ops" in x
               for x in idx)
    # the numeric comma-stripped path gets its own expression index
    assert any("replace(title, ',', '')" in x for x in idx)
