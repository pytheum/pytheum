# Mirror of pytheum_matcher.match.fungibility — keep in sync (shared test vectors).
"""
Settlement-fungibility classification for verified cross-venue pairs.

A pair can be a true cross-venue EQUIVALENCE (same question, same answer) yet still
NOT be *arbitrage-clean*, because the two venues may settle on different reference
sources, different settlement math, or different timing. This module labels each
verified pair with how cleanly its equivalence translates into a lockable position:

  arbitrage_clean   — both venues resolve to the SAME objective real-world outcome,
                      effectively lockable. (Single-game sports, props, official data
                      releases.) Residual caveat: regulation-vs-OT scope + the lag of
                      Polymarket's UMA propose/dispute window (non-simultaneous, but the
                      outcome is locked once the event concludes).
  correlated        — price-LINKED but with basis risk: the venues reference different
                      indices and/or settlement math, so they can land on opposite sides
                      of a strike. Canonical case: crypto price markets (Kalshi = CF
                      Benchmarks RTI 60-second average; Polymarket = Chainlink point-in-
                      time read). Basis risk grows as market duration shrinks.
  timing_divergent  — same EVENTUAL outcome, but resolution *criteria/timing* differ
                      enough that the two legs are not simultaneously lockable (elections:
                      AP call vs official certification; UMA dispute windows).

This is EQUIVALENCE-QUALITY metadata (the matcher's job: are these two truly the same
market, and how cleanly), NOT a trading signal. Sources + the doc-verified settlement
table behind these rules: docs/research/2026-06-29-prediction-market-fees-and-settlement.md
"""
from __future__ import annotations

import re

ARBITRAGE_CLEAN = "arbitrage_clean"
CORRELATED = "correlated"
TIMING_DIVERGENT = "timing_divergent"

#: Static, per-class rationale (do NOT emit per-row — keep the export compact; a consumer
#: surfaces this legend alongside the one-word class label).
FUNGIBILITY_RATIONALE: dict[str, str] = {
    ARBITRAGE_CLEAN: (
        "Both venues resolve to the same objective real-world outcome; lockable. "
        "Caveat: confirm regulation-vs-OT scope; Polymarket's UMA propose/dispute window "
        "means settlement is not simultaneous (the outcome is fixed once the event concludes)."
    ),
    CORRELATED: (
        "Price-linked with basis risk: Kalshi settles on CF Benchmarks RTI (60-second "
        "average), Polymarket on Chainlink (point-in-time read) — different index AND "
        "settlement math, so the legs can land on opposite sides of a strike. Basis risk "
        "grows as duration shrinks. Correlated, not arbitrage-clean."
    ),
    TIMING_DIVERGENT: (
        "Same eventual outcome but resolution criteria/timing differ (e.g. AP call vs "
        "official certification; Polymarket UMA dispute windows), so the two legs are not "
        "simultaneously lockable."
    ),
}

# Single-game / objective-outcome sports + props. These are listed EXPLICITLY only so a
# sports market whose name happens to contain a crypto token (e.g. "Bitcoin FC") is not
# misread as crypto — they beat the crypto title-keyword fallback below. Any objective
# bet_type NOT listed here still resolves ARBITRAGE_CLEAN via the default (see classify),
# so the set need not be exhaustive; it just protects against crypto-name false positives.
_SPORTS_BET_TYPES = frozenset({
    "moneyline", "moneyline_outcome", "moneyline_1h",
    "total", "total_1h", "total_2h", "total_f5", "spread", "spread_1h",
    "btts", "nrfi", "team_total", "team_corners", "halftime", "extra_innings",
    "tennis_ml", "tennis_set1", "tennis_total", "tennis_set_total",
    "esports_map", "esports_series", "esports_total",
    "prop", "player_prop", "nfl_prop", "wc_prop", "mlb_prop", "goalscorer",
    "pga_top", "ufc_ml", "ufc_distance",
    "winter_olympics_gold", "division_winner", "win_total",
})
# Elections / one-off political events: call-criteria (AP call vs certification) + UMA
# dispute timing diverge → not simultaneously lockable.
_EVENT_BET_TYPES = frozenset({"event", "house_party"})

_CRYPTO_RE = re.compile(
    r"\b(bitcoin|btc|ethereum|ether|solana|crypto|dogecoin|xrp|ripple)\b", re.IGNORECASE
)


def classify_fungibility(
    bet_type: str | None = None,
    *,
    kalshi_title: str = "",
    pm_title: str = "",
    slice_: str = "",
) -> str:
    """
    Classify how cleanly a verified-equivalent pair translates into a lockable position.

    Returns one of ARBITRAGE_CLEAN / CORRELATED / TIMING_DIVERGENT.

    Default is ARBITRAGE_CLEAN: this corpus is VERIFIED 1:1 equivalences, and for an
    objective real-world outcome (a game/match/season result, an official data figure)
    equivalence ⇒ same settlement ⇒ lockable. Only two families carry systematic
    non-clean risk, applied as explicit overrides:
      - CORRELATED  — crypto price markets (different reference index + averaging-vs-tick).
      - TIMING_DIVERGENT — elections / political events (call-criteria + UMA timing).
    Precedence: known sports beat the crypto title-keyword (so "Bitcoin FC" stays clean);
    a crypto signal beats the event bucket (so a bitcoin market mis-bucketed as 'event'
    is still flagged correlated).
    """
    bt = (bet_type or "").strip().lower()
    sl = (slice_ or "").strip().lower()

    # 1. Explicit crypto bet_type/slice — basis risk.
    if "crypto" in bt or "crypto" in sl:
        return CORRELATED

    # 2. Known objective sports/props — clean, and protected from crypto-name false positives.
    if bt in _SPORTS_BET_TYPES:
        return ARBITRAGE_CLEAN

    # 3. Crypto price by title keyword (only reached for non-sports) — basis risk; beats events.
    if _CRYPTO_RE.search(f"{kalshi_title} {pm_title}"):
        return CORRELATED

    # 4. Elections / political events — call-criteria + timing diverge.
    if bt in _EVENT_BET_TYPES:
        return TIMING_DIVERGENT

    # 5. Default — objective-outcome equivalence is lockable.
    return ARBITRAGE_CLEAN
