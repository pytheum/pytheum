"""Structured resolution-field extraction + settlement-divergence detection.

Upgrades the MM fungibility verdict from a method/confidence PROXY to a HARD signal by
parsing each leg's resolution-rules text into comparable structured fields and diffing them.

The #1 prediction-market MM risk (2026 settlement-dispute crisis) is two "matched" contracts
that resolve DIFFERENTLY — a different numeric threshold, a different settlement source/oracle,
or a different cutoff. A cross-venue hedge on such a pair is a landmine: both legs can pay the
same side. This detects exactly that from the rules text Pytheum already ships side-by-side
(``t_market_rules`` / GET /v1/markets/{ref}/rules).

v1 is a CONSERVATIVE, deterministic heuristic (regex over thresholds / named sources) — it is
tuned to UNDER-flag, because a false "not fungible" needlessly kills a good hedge, while the
cases it does catch (threshold or source mismatch) are the classic non-fungible traps. An LLM
semantic-criterion comparator is the v2 drop-in behind the same ``settlement_divergence(...)``
interface (for cases regex can't see, e.g. "inaugurated" vs "wins the election").

stdlib only.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

# Named settlement sources / oracles a resolution rule commonly cites. Disjoint sources across
# the two legs = a real settlement-risk signal (e.g. Kalshi's official source vs Polymarket's UMA).
_SOURCE_LEXICON: dict[str, str] = {
    "associated press": "AP", " ap ": "AP", "espn": "ESPN", "bloomberg": "Bloomberg",
    "reuters": "Reuters", "uma": "UMA", "optimistic oracle": "UMA", "coingecko": "CoinGecko",
    "coinbase": "Coinbase", "binance": "Binance", "chainlink": "Chainlink",
    "bureau of labor": "BLS", " bls ": "BLS", "federal reserve": "Federal Reserve",
    "fomc": "FOMC", "sec.gov": "SEC", "decision desk": "DecisionDeskHQ", "edison": "Edison",
    "mlb.com": "MLB", "nba.com": "NBA", "nfl.com": "NFL", "usgs": "USGS",
}
# A number is a "threshold" when a comparator precedes it within a short window.
_COMPARATOR = (r"(?:>=|<=|>|<|≥|≤|at least|no less than|greater than|more than|higher than|"
               r"above|below|under|less than|exceed(?:s|ing)?|reach(?:es)?|hit(?:s)?)")
_THRESHOLD_RE = re.compile(_COMPARATOR + r"\s*\$?\s*([0-9]{1,7}(?:\.[0-9]+)?)\s*(%?)", re.I)


@dataclass(frozen=True)
class ResolutionFields:
    thresholds: frozenset[float]   # numeric thresholds found next to a comparator
    sources: frozenset[str]        # named settlement sources / oracles
    raw_len: int                   # length of the rules text (0 = no rules available)

    @property
    def has_rules(self) -> bool:
        return self.raw_len > 0


def extract_fields(text: str | None) -> ResolutionFields:
    """Parse resolution-rules text into comparable structured fields (conservative heuristic)."""
    t = (text or "")
    tl = f" {t.lower()} "
    sources = frozenset(v for k, v in _SOURCE_LEXICON.items() if k in tl)
    thresholds = set()
    for m in _THRESHOLD_RE.finditer(t):
        val = float(m.group(1))
        # keep percentages and bare values as-is; round to avoid float noise on the compare
        thresholds.add(round(val, 4))
    return ResolutionFields(frozenset(thresholds), sources, len(t))


def settlement_divergence(a: ResolutionFields, b: ResolutionFields) -> tuple[bool, list[str]]:
    """(divergent, reasons). Flags only STRONG evidence that the two legs resolve differently:
    disjoint numeric thresholds, or disjoint named settlement sources. Returns (False, []) when
    rules are missing or the evidence is inconclusive (under-flag by design)."""
    reasons: list[str] = []
    if a.thresholds and b.thresholds and a.thresholds.isdisjoint(b.thresholds):
        reasons.append(f"threshold mismatch: {sorted(a.thresholds)} vs {sorted(b.thresholds)}")
    if a.sources and b.sources and a.sources.isdisjoint(b.sources):
        reasons.append(f"settlement source differs: {sorted(a.sources)} vs {sorted(b.sources)}")
    return (bool(reasons), reasons)


def divergence_from_text(a_text: str | None, b_text: str | None) -> tuple[bool, list[str]]:
    """Convenience: extract both legs' fields from raw rules text and diff. Returns (False, [])
    if either side has no rules text (can't compare → don't false-flag)."""
    fa, fb = extract_fields(a_text), extract_fields(b_text)
    if not (fa.has_rules and fb.has_rules):
        return False, []
    return settlement_divergence(fa, fb)
