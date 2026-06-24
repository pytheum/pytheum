"""Shared pytest fixtures and sys.path setup for the pytheum test suite."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

# Make the scripts/ directory importable so dataset-script tests can do
# ``import gen_checksums`` and ``import verify_checksums`` without installing.
_SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))


@pytest.fixture(autouse=True)
def _clear_screen_cache():
    """Isolate the module-level /v1/markets/screen response cache between tests.

    handle_markets_screen now memoizes successful results in a process-wide
    param-keyed cache (mirrors markets_equivalents._cache). Many screen tests
    call with the same default params ({}) but distinct fake DAOs, so without a
    reset the second test would hit the first's cached body. Cleared before each
    test so every test sees a cold cache."""
    from pytheum.api.markets_screen import _screen_cache
    _screen_cache.clear()
    yield
