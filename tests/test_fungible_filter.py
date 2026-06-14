"""Tests for the settlement-fungibility filter.

Covers:
1. is_fungible_method() unit tests — fungible vs non-fungible method values.
2. EquivalenceIndex.browse(fungible_only=True) — correct in-memory filtering.
3. handle_markets_matched with fungible_only=True — meta includes fungible_excluded.
4. handle_markets_equivalents (collection) with fungible_only=True — filtered pairs.
"""
from __future__ import annotations

import pytest

from pytheum.equivalence.index import EquivalenceIndex, is_fungible_method

# ---------------------------------------------------------------------------
# Shared fake pair data
# ---------------------------------------------------------------------------

_DET_PAIR = {
    "kalshi_ref": "kalshi:KX-NBA-LAL-BOS",
    "kalshi_ticker": "KX-NBA-LAL-BOS",
    "pm_ref": "polymarket:10001",
    "pm_gamma_id": "10001",
    "pm_slug": "lakers-celtics",
    "bet_type": "moneyline",
    "method": "structured_key",
    "confidence": 1.0,
    "kalshi_title": "Will the Lakers beat the Celtics?",
    "pm_title": "Lakers vs Celtics winner",
}

_LLM_PAIR = {
    "kalshi_ref": "kalshi:KX-ELECTION-2026",
    "kalshi_ticker": "KX-ELECTION-2026",
    "pm_ref": "polymarket:10002",
    "pm_gamma_id": "10002",
    "pm_slug": "us-election-2026",
    "bet_type": "event",
    "method": "opus_backstop",
    "confidence": 0.91,
    "kalshi_title": "Will Democrats win 2026 Senate?",
    "pm_title": "Democrats 2026 Senate?",
}

_MIXED_PAIR = {
    "kalshi_ref": "kalshi:KX-MIXED",
    "kalshi_ticker": "KX-MIXED",
    "pm_ref": "polymarket:10003",
    "pm_gamma_id": "10003",
    "pm_slug": "mixed-pair",
    "bet_type": "event",
    "method": "blocked_deterministic,opus_backstop",
    "confidence": 0.95,
    "kalshi_title": "Mixed method pair",
    "pm_title": "Mixed method pair",
}

_HUMAN_PAIR = {
    "kalshi_ref": "kalshi:KX-HUMAN",
    "kalshi_ticker": "KX-HUMAN",
    "pm_ref": "polymarket:10004",
    "pm_gamma_id": "10004",
    "pm_slug": "human-pair",
    "bet_type": "event",
    "method": "human_adjudicated",
    "confidence": 1.0,
    "kalshi_title": "Human adjudicated pair",
    "pm_title": "Human adjudicated pair",
}


def _make_index(pairs: list[dict] | None = None) -> EquivalenceIndex:
    """Build an EquivalenceIndex in-memory without touching disk."""
    idx = EquivalenceIndex()
    idx.dataset_version = "2026-06-12T00:00:00Z"
    for row in (pairs if pairs is not None else [_DET_PAIR, _LLM_PAIR, _MIXED_PAIR, _HUMAN_PAIR]):
        idx._rows.append(row)
        kt = row.get("kalshi_ticker")
        if kt:
            idx._by_kalshi_ticker.setdefault(kt, []).append(row)
        gid = row.get("pm_gamma_id")
        if gid is not None:
            idx._by_pm_gamma_id.setdefault(str(gid), []).append(row)
        slug = row.get("pm_slug")
        if slug:
            idx._by_pm_slug.setdefault(slug, []).append(row)
    return idx


class _SimpleDao:
    def __init__(self, store: dict | None = None) -> None:
        self._store: dict = store or {}

    async def fetch_market(self, ref: str) -> dict | None:
        return self._store.get(ref)

    async def fetch_markets_by_ids(self, ids: list[str]) -> list[dict]:
        return [self._store[ref] for ref in ids if ref in self._store]

    async def fetch_equivalence_pairs(self, limit: int = 50) -> list[dict]:
        return []


# ---------------------------------------------------------------------------
# 1. is_fungible_method() unit tests
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("method,expected", [
    # Fungible — deterministic / structural methods
    ("structured_key", True),
    ("blocked_deterministic", True),
    ("human_adjudicated", True),
    ("award_match", True),
    ("election_match", True),
    ("macro_match", True),
    ("game_match", True),
    ("game_title_match", True),
    ("game_datewindow_match", True),
    ("tennis_match", True),
    ("total_match", True),
    ("spread_match", True),
    ("btts_match", True),
    ("player_prop_match", True),
    ("ufc_ml_match", True),
    ("pga_top_match", True),
    # NOT fungible — LLM / backstop methods
    ("opus_backstop", False),
    ("llm_local", False),
    ("llm_judge", False),
    # NOT fungible — combined method that includes LLM component
    ("blocked_deterministic,opus_backstop", False),
    ("structured_key,llm_local", False),
    ("game_match,llm_judge", False),
    # NOT fungible — None / empty
    (None, False),
    ("", False),
    ("  ", False),
])
def test_is_fungible_method(method: str | None, expected: bool):
    assert is_fungible_method(method) is expected


# ---------------------------------------------------------------------------
# 2. EquivalenceIndex.browse(fungible_only=True)
# ---------------------------------------------------------------------------


def test_browse_fungible_only_excludes_llm_pairs():
    """browse(fungible_only=True) should return only deterministic/human pairs."""
    idx = _make_index()
    rows, total = idx.browse(fungible_only=True)
    methods = [r.get("method") for r in rows]
    assert "structured_key" in methods
    assert "human_adjudicated" in methods
    assert "opus_backstop" not in methods
    assert "blocked_deterministic,opus_backstop" not in methods
    assert total == 2


def test_browse_fungible_only_false_includes_all():
    """browse(fungible_only=False) is the default — returns all rows."""
    idx = _make_index()
    _, total_all = idx.browse(fungible_only=False)
    _, total_default = idx.browse()
    assert total_all == 4
    assert total_default == 4


def test_browse_fungible_only_total_vs_all():
    """Total from fungible_only=True should be strictly less than all rows
    when there are LLM-judged pairs in the dataset."""
    idx = _make_index()
    _, total_fungible = idx.browse(fungible_only=True)
    _, total_all = idx.browse(fungible_only=False)
    assert total_fungible < total_all


def test_browse_fungible_only_with_other_filters():
    """fungible_only=True stacks with other filters like bet_types."""
    idx = _make_index()
    rows, total = idx.browse(bet_types={"moneyline"}, fungible_only=True)
    assert total == 1
    assert rows[0]["method"] == "structured_key"


# ---------------------------------------------------------------------------
# 3. handle_markets_matched with fungible_only=True
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_handle_markets_matched_fungible_only_true():
    """fungible_only=True restricts to deterministic pairs; meta includes fungible_excluded."""
    from pytheum.api.markets_matched import handle_markets_matched

    idx = _make_index()
    dao = _SimpleDao()
    status, body = await handle_markets_matched(
        {"fungible_only": "true"},
        dao=dao,
        equivalence=idx,
    )
    assert status == 200
    methods = [p.get("method") for p in body["pairs"]]
    assert "opus_backstop" not in methods
    assert "blocked_deterministic,opus_backstop" not in methods
    assert body["meta"]["filter"]["fungible_only"] is True
    assert "fungible_excluded" in body["meta"]
    assert body["meta"]["fungible_excluded"] > 0


@pytest.mark.asyncio
async def test_handle_markets_matched_fungible_only_false_no_excluded_key():
    """When fungible_only=False (default), meta should NOT include fungible_excluded."""
    from pytheum.api.markets_matched import handle_markets_matched

    idx = _make_index()
    dao = _SimpleDao()
    status, body = await handle_markets_matched(
        {},
        dao=dao,
        equivalence=idx,
    )
    assert status == 200
    assert "fungible_excluded" not in body["meta"]
    assert body["meta"]["filter"]["fungible_only"] is False


@pytest.mark.asyncio
async def test_handle_markets_matched_fungible_only_truthy_strings():
    """'1' and 'yes' are accepted as fungible_only=True."""
    from pytheum.api.markets_matched import handle_markets_matched

    idx = _make_index()
    dao = _SimpleDao()
    for val in ("1", "yes", "true", "TRUE", "Yes"):
        _, body = await handle_markets_matched(
            {"fungible_only": val},
            dao=dao,
            equivalence=idx,
        )
        assert body["meta"]["filter"]["fungible_only"] is True, f"expected True for {val!r}"


@pytest.mark.asyncio
async def test_handle_markets_matched_fungible_only_excluded_count_correct():
    """fungible_excluded = total_all - total_fungible for the given other filters."""
    from pytheum.api.markets_matched import handle_markets_matched

    idx = _make_index()
    dao = _SimpleDao()
    _, body = await handle_markets_matched(
        {"fungible_only": "true"},
        dao=dao,
        equivalence=idx,
    )
    assert body["meta"]["fungible_excluded"] == 2


# ---------------------------------------------------------------------------
# 4. handle_markets_equivalents collection with fungible_only=True
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_handle_markets_equivalents_fungible_only():
    """Collection endpoint: fungible_only=true restricts pairs served."""
    from pytheum.api.markets_equivalents import _cache, handle_markets_equivalents

    _cache.clear()

    idx = _make_index()
    dao = _SimpleDao()
    status, body = await handle_markets_equivalents(
        {"fungible_only": "true"},
        dao=dao,
        equivalence=idx,
        force_refresh=True,
    )
    assert status == 200
    for pair in body.get("pairs", []):
        assert is_fungible_method(pair.get("method")), (
            f"non-fungible pair in result: {pair.get('method')!r}"
        )
    assert body["meta"]["fungible_only"] is True


@pytest.mark.asyncio
async def test_handle_markets_equivalents_fungible_only_false_default():
    """fungible_only defaults to False; meta echoes it."""
    from pytheum.api.markets_equivalents import _cache, handle_markets_equivalents

    _cache.clear()
    idx = _make_index()
    dao = _SimpleDao()
    _, body = await handle_markets_equivalents(
        {},
        dao=dao,
        equivalence=idx,
        force_refresh=True,
    )
    assert body["meta"]["fungible_only"] is False


@pytest.mark.asyncio
async def test_handle_markets_equivalents_fungible_only_cache_key_varies():
    """Cache hits are keyed by (limit, fungible_only, include_rules) independently."""
    from pytheum.api.markets_equivalents import _cache, handle_markets_equivalents

    _cache.clear()
    idx = _make_index()
    dao = _SimpleDao()

    _, body_all = await handle_markets_equivalents(
        {"limit": "50"},
        dao=dao,
        equivalence=idx,
        force_refresh=True,
    )
    _, body_fungible = await handle_markets_equivalents(
        {"limit": "50", "fungible_only": "true"},
        dao=dao,
        equivalence=idx,
        force_refresh=True,
    )
    assert body_fungible["meta"]["fungible_only"] is True
    assert body_all["meta"]["fungible_only"] is False
