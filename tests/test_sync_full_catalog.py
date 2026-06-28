"""Unit tests for the pure row mappers in scripts.sync_full_catalog."""
import json

from scripts.sync_full_catalog import kalshi_catalog_row, polymarket_catalog_row


def test_kalshi_row_cents_to_unit_id_and_twin_flag():
    raw = json.dumps({"yes_bid": 40, "yes_ask": 43, "last_price": 41,
                      "event_ticker": "KXFOO", "volume_fp": 1000})
    row = kalshi_catalog_row("KXFOO-26-A", "Foo?", "active",
                             "2026-12-31T00:00:00Z", raw, twin=True)
    assert row[0] == "kalshi:KXFOO-26-A"      # serving ref
    assert row[2] == "kalshi" and row[3] == "active"
    p = json.loads(row[8])
    assert p["bestBid"] == 0.40 and p["bestAsk"] == 0.43 and p["lastTradePrice"] == 0.41
    assert p["has_verified_twin"] is True
    assert p["synced_by"] == "full_catalog"


def test_polymarket_row_uses_gamma_id_not_condition_id():
    raw = json.dumps({"id": "701486", "slug": "will-x", "conditionId": "0xabc",
                      "bestBid": 0.6, "bestAsk": 0.62, "volumeNum": 500,
                      "endDate": "2026-06-30T00:00:00Z", "question": "Will X?"})
    row = polymarket_catalog_row("0xabc", "Will X?", "active", None, raw, twin=False)
    assert row[0] == "polymarket:701486"      # gamma id, NOT the conditionId
    assert row[2] == "polymarket" and row[3] == "active"
    p = json.loads(row[8])
    assert p["bestBid"] == 0.6 and p["bestAsk"] == 0.62   # PM gamma already [0,1]
    assert p["condition_id"] == "0xabc"
    assert p["has_verified_twin"] is False


def test_polymarket_row_none_without_gamma_id():
    raw = json.dumps({"conditionId": "0xabc", "slug": "x"})  # no 'id'
    assert polymarket_catalog_row("0xabc", "X", "active", None, raw, twin=False) is None


def test_status_maps_to_closed_when_not_active():
    row = kalshi_catalog_row("KXA-1", "t", "finalized", None, "{}", twin=False)
    assert row[3] == "closed"
