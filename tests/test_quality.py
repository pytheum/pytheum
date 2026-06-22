"""/v1/quality (t_quality) — dataset quality + integrity transparency."""

from __future__ import annotations

import pytheum.api.quality as quality_mod
from pytheum.api.quality import handle_quality
from pytheum.equivalence.index import EquivalenceIndex


def _idx(rows: list[dict]) -> EquivalenceIndex:
    idx = EquivalenceIndex()
    idx._rows = rows
    idx.dataset_version = "2026-06-22T00:00:00Z"
    return idx


def test_quality_stats_splits_tiers_and_tallies_composition() -> None:
    s = _idx([
        {"method": "structured_key", "bet_type": "moneyline"},
        {"method": "total_match", "bet_type": "total"},
        {"method": "opus_backstop", "bet_type": "event"},  # LLM-judged
    ]).quality_stats()
    assert s["pairs_total"] == 3
    assert s["tiers"]["fungible"]["pairs"] == 2     # structured_key + total_match
    assert s["tiers"]["judged"]["pairs"] == 1       # opus_backstop
    assert s["tiers"]["fungible"]["pct"] == round(100 * 2 / 3, 1)
    assert s["by_bet_type"]["total"] == 1
    assert s["bet_types_total"] == 3
    assert s["dataset_version"] == "2026-06-22T00:00:00Z"


async def test_handle_quality_adds_integrity_and_honest_precision() -> None:
    quality_mod._cache = None
    _, body = await handle_quality(
        {}, equivalence=_idx([{"method": "game_match", "bet_type": "moneyline"}]))
    assert body["pairs_total"] == 1
    assert body["integrity"]["enforced_at_build"] is True
    assert len(body["integrity"]["invariants"]) >= 3
    # honest, tier-scoped audited precision — judged tier measured, deterministic pending
    aud = body["precision"]["audited"]
    assert aud["judged_tier_pct"] == 98.7
    assert aud["judged_tier_ci95"] == [95.4, 99.7]
    assert aud["deterministic_tier_pct"] is None      # gated, not yet sampled-audited
    assert "version" in body["service"]


async def test_handle_quality_degrades_without_an_index() -> None:
    quality_mod._cache = None

    class _NoStats:
        pass

    _, body = await handle_quality({}, equivalence=_NoStats())
    assert body["pairs_total"] == 0          # graceful zeros
    assert body["integrity"]["enforced_at_build"] is True  # static block still present
