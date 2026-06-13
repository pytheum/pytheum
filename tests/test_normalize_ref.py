"""Tests for normalize_ref URL extraction and ref normalization.

Covers:
  - URL extraction for Kalshi and Polymarket
  - Tolerance of query strings, trailing slashes, https/http
  - Case folding of venue prefix
  - Pass-through for already-normalized refs and unrecognized URLs
  - Per-endpoint smoke tests: equivalents, rules, related accept URLs
"""
from __future__ import annotations

import pytest

from pytheum.api.ref_utils import normalize_ref


# ---------------------------------------------------------------------------
# normalize_ref unit tests
# ---------------------------------------------------------------------------


def test_kalshi_markets_url():
    assert normalize_ref("https://kalshi.com/markets/KXFED-25-MAY") == "kalshi:KXFED-25-MAY"


def test_kalshi_markets_url_with_query():
    assert normalize_ref("https://kalshi.com/markets/KXFED-25-MAY?ref=foo") == "kalshi:KXFED-25-MAY"


def test_kalshi_markets_url_with_trailing_slash():
    assert normalize_ref("https://kalshi.com/markets/KXFED-25-MAY/") == "kalshi:KXFED-25-MAY"


def test_kalshi_events_url():
    assert normalize_ref("https://kalshi.com/events/KXFED") == "kalshi:KXFED"


def test_kalshi_events_url_www():
    assert normalize_ref("https://www.kalshi.com/events/KXNBA-26") == "kalshi:KXNBA-26"


def test_kalshi_url_http():
    assert normalize_ref("http://kalshi.com/markets/KXTEST-01") == "kalshi:KXTEST-01"


def test_polymarket_event_url():
    assert normalize_ref("https://polymarket.com/event/will-the-fed-cut-rates") == \
        "polymarket:will-the-fed-cut-rates"


def test_polymarket_market_url():
    assert normalize_ref("https://polymarket.com/market/some-slug") == "polymarket:some-slug"


def test_polymarket_url_with_query():
    assert normalize_ref("https://polymarket.com/event/some-slug?ref=bar") == \
        "polymarket:some-slug"


def test_polymarket_url_with_trailing_slash():
    assert normalize_ref("https://polymarket.com/event/some-slug/") == "polymarket:some-slug"


def test_polymarket_url_www():
    assert normalize_ref("https://www.polymarket.com/event/slug-here") == "polymarket:slug-here"


def test_unrecognized_url_passthrough():
    """Unknown domain URL passes through as-is."""
    url = "https://example.com/markets/KXTEST"
    assert normalize_ref(url) == url


def test_already_prefixed_kalshi():
    assert normalize_ref("kalshi:KXFED-25-MAY") == "kalshi:KXFED-25-MAY"


def test_already_prefixed_polymarket():
    assert normalize_ref("polymarket:558936") == "polymarket:558936"


def test_case_folds_venue_prefix():
    assert normalize_ref("KALSHI:KXFED-25-MAY") == "kalshi:KXFED-25-MAY"
    assert normalize_ref("Polymarket:558936") == "polymarket:558936"


def test_strips_whitespace():
    assert normalize_ref("  kalshi:KXFED-25-MAY  ") == "kalshi:KXFED-25-MAY"


def test_bare_ticker_passthrough():
    """A bare ticker with no prefix passes through unchanged."""
    assert normalize_ref("KXFED-25-MAY") == "KXFED-25-MAY"


def test_empty_string():
    assert normalize_ref("") == ""


def test_whitespace_only():
    assert normalize_ref("   ") == ""


def test_case_fold_with_whitespace_after_colon():
    """'KALSHI: KXFED' → 'kalshi:KXFED' (body whitespace stripped)."""
    assert normalize_ref("KALSHI: KXFED-25-MAY") == "kalshi:KXFED-25-MAY"


# ---------------------------------------------------------------------------
# Per-endpoint integration: equivalents accepts URLs
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_equivalents_handler_accepts_kalshi_url():
    """handle_market_equivalents should normalize a Kalshi URL before lookup."""
    from pytheum.api.markets_equivalents import handle_market_equivalents
    from pytheum.equivalence.index import EquivalenceIndex

    idx = EquivalenceIndex()
    idx._rows = [{
        "kalshi_ticker": "KXFED-25-MAY",
        "pm_ref": "polymarket:10001",
        "pm_gamma_id": "10001",
        "kalshi_title": "Will the Fed cut in May?",
        "pm_title": "Fed cut May?",
        "bet_type": "event",
        "confidence": 1.0,
        "method": "blocked_deterministic",
    }]
    idx._by_kalshi_ticker["KXFED-25-MAY"] = idx._rows

    class _NullDao:
        async def fetch_market(self, ref):
            return None

    _, body = await handle_market_equivalents(
        "https://kalshi.com/markets/KXFED-25-MAY",
        {},
        dao=_NullDao(),
        equivalence=idx,
    )
    assert len(body["equivalents"]) == 1


@pytest.mark.asyncio
async def test_equivalents_handler_accepts_polymarket_url():
    """handle_market_equivalents normalizes a Polymarket event URL."""
    from pytheum.api.markets_equivalents import handle_market_equivalents
    from pytheum.equivalence.index import EquivalenceIndex

    idx = EquivalenceIndex()
    row = {
        "kalshi_ticker": "KXFED-25-MAY",
        "kalshi_ref": "kalshi:KXFED-25-MAY",
        "pm_ref": "polymarket:fed-cut-may",
        "pm_slug": "fed-cut-may",
        "kalshi_title": "Will the Fed cut in May?",
        "pm_title": "Fed cut May?",
        "bet_type": "event",
        "confidence": 1.0,
        "method": "blocked_deterministic",
    }
    idx._rows = [row]
    idx._by_pm_slug["fed-cut-may"] = [row]

    class _NullDao:
        async def fetch_market(self, ref):
            return None

    _, body = await handle_market_equivalents(
        "https://polymarket.com/event/fed-cut-may",
        {},
        dao=_NullDao(),
        equivalence=idx,
    )
    assert len(body["equivalents"]) == 1


@pytest.mark.asyncio
async def test_rules_handler_accepts_url():
    """handle_market_rules normalizes a URL before lookup."""
    from pytheum.api.markets_rules import handle_market_rules
    from pytheum.equivalence.index import EquivalenceIndex

    idx = EquivalenceIndex()
    idx._rows = [{
        "kalshi_ticker": "KXFED-25-MAY",
        "pm_ref": "polymarket:10001",
        "pm_gamma_id": "10001",
        "kalshi_title": "Will the Fed cut in May?",
        "pm_title": "Fed cut May?",
        "bet_type": "event",
        "confidence": 1.0,
        "method": "blocked_deterministic",
    }]
    idx._by_kalshi_ticker["KXFED-25-MAY"] = idx._rows

    class _NullDao:
        async def fetch_market(self, ref):
            return None

    _, body = await handle_market_rules(
        "https://kalshi.com/markets/KXFED-25-MAY",
        {},
        dao=_NullDao(),
        equivalence=idx,
    )
    assert body["comparison"]["confidence"] == 1.0


@pytest.mark.asyncio
async def test_related_handler_accepts_url():
    """handle_market_related normalizes a URL before lookup."""
    from pytheum.api.markets_related import handle_market_related
    from pytheum.related.index import RelatedIndex

    rel = RelatedIndex()
    rel._rows = []
    rel._by_kalshi_ticker = {}
    rel._by_pm_slug = {}

    class _NullDao:
        async def fetch_market(self, ref):
            return None

    _, body = await handle_market_related(
        "https://kalshi.com/markets/KXFED-25-MAY",
        {},
        dao=_NullDao(),
        related=rel,
    )
    assert body["related"] == []
