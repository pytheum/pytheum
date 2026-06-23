"""find_divergences resolution-date integrity.

Polymarket's endDate is a known-unreliable placeholder on some markets (verified via
gamma: the NBA-draft PM legs carry 2026-06-24 while the real draft / Kalshi twin is
2026-07-07). That ~2-week gap would poison annualized_net_edge — a sub-day "lock"
annualizes to the 1000.0 cap and dominates the sort. When verified-equivalent legs
disagree by >1 day we flag `resolution_mismatch` and annualize over the trusted Kalshi
horizon instead of the placeholder.
"""

from __future__ import annotations

import pytheum.mcp.tools as tools
from pytheum.mcp.tools import find_divergences


def _pair(k_days: float, p_days: float) -> dict:
    return {
        "bet_type": "event", "poly_side": None, "method": "opus_backstop", "confidence": 1.0,
        "a": {"venue": "kalshi", "question": "Will Caleb Wilson be the 4th overall pick?",
              "days_to_resolution": k_days, "book": {"bid": 0.80, "ask": 0.82}},
        "b": {"venue": "polymarket", "question": "Will Caleb Wilson be the 4th overall pick?",
              "days_to_resolution": p_days, "book": {"bid": 0.70, "ask": 0.72}},
    }


async def test_mismatch_flagged_and_annualized_over_kalshi_horizon(monkeypatch) -> None:
    async def _fake_get(path, params, base_url):  # noqa: ANN001
        return {"pairs": [_pair(k_days=14.0, p_days=0.67)]}  # PM placeholder ~0.67d

    monkeypatch.setattr(tools, "_get", _fake_get)
    out = await find_divergences(min_net_edge=-1.0, limit=10, include_rules=False)
    d = out["divergences"][0]
    assert d["resolution_mismatch"] is True
    # lock_days uses the trusted Kalshi horizon (14), NOT the 0.67 placeholder.
    assert d["lock_days"] == 14.0


async def test_no_mismatch_when_dates_agree(monkeypatch) -> None:
    async def _fake_get(path, params, base_url):  # noqa: ANN001
        return {"pairs": [_pair(k_days=100.0, p_days=100.0)]}

    monkeypatch.setattr(tools, "_get", _fake_get)
    out = await find_divergences(min_net_edge=-1.0, limit=10, include_rules=False)
    d = out["divergences"][0]
    assert d["resolution_mismatch"] is False
    assert d["lock_days"] == 100.0  # binding horizon = later leg (both equal here)
