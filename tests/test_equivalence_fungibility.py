import pytest
from pytheum.equivalence.fungibility import (
    classify_fungibility, ARBITRAGE_CLEAN, CORRELATED, TIMING_DIVERGENT)

@pytest.mark.parametrize("bt", ["moneyline","total","spread","tennis_ml","prop","team_total"])
def test_sports_clean(bt):
    assert classify_fungibility(bt, kalshi_title="A vs B", pm_title="A vs. B") == ARBITRAGE_CLEAN

@pytest.mark.parametrize("bt", ["event","house_party"])
def test_events_timing(bt):
    assert classify_fungibility(bt) == TIMING_DIVERGENT

def test_crypto_correlated():
    assert classify_fungibility(None, kalshi_title="Bitcoin above $100k?", pm_title="BTC") == CORRELATED
    assert classify_fungibility("event", kalshi_title="Bitcoin price", pm_title="BTC") == CORRELATED

def test_unknown_defaults_clean():
    assert classify_fungibility("new_type") == ARBITRAGE_CLEAN
