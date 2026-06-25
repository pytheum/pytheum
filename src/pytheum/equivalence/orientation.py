"""Cross-venue pair orientation — the single source of truth for "which Polymarket outcome
corresponds to the Kalshi YES side".

A matched pair links MARKET↔MARKET, but the Polymarket leg's implied_yes is its FIRST-LISTED
outcome, which may be the OTHER side than the Kalshi ticker's YES. Edge-scoring (t_find_divergences)
needs the legs pointing at the SAME side or the net edge is inverted — so orientation is
precision-critical: a wrong map is worse than no map (it surfaces a phantom/backwards arb).

Two proposition shapes are oriented here (conservatively — ambiguity returns None, never a guess):
  - team/player WIN  (moneyline/tennis_ml/esports_*): the Kalshi YES TEAM (parsed from a
    'Will X win ...' title, which names only the YES side) maps to the PM outcome it token-matches.
  - over/under TOTAL: the Kalshi YES DIRECTION (over/under, from the yes_sub_title) maps to the
    PM outcome of the same direction.

Used both at rest (scripts/map_pair_sides → pair_side_map) and at serve (markets_equivalents
computes poly_side inline so the perishable daily front is oriented without a pre-computed table).
"""
from __future__ import annotations

import json
import re
from typing import Any

_WIN_TITLE = re.compile(r"\bwill\s+(.+?)\s+win\b", re.IGNORECASE)


def outcomes_from_payload(payload: Any) -> list[str] | None:
    """The Polymarket `outcomes` name list from a market payload (str or dict; the value may
    itself be a JSON-encoded string). None if absent/malformed."""
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except ValueError:
            return None
    raw = (payload or {}).get("outcomes")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            return None
    if isinstance(raw, list) and all(isinstance(x, str) for x in raw):
        return raw
    return None

# team/player-WIN proposition: token-match the YES team to a PM outcome.
TEAM_BET_TYPES = frozenset({
    "moneyline", "moneyline_outcome", "tennis_ml", "esports_series", "esports_map",
})
# over/under proposition: direction-match (line already matched by the matcher).
TOTAL_BET_TYPES = frozenset({
    "total", "total_1h", "team_total", "tennis_total", "esports_total", "wc_2h_total",
})


def yes_team_from_title(title: str | None) -> str | None:
    """The YES team from a 'Will <TEAM> win the <A> vs <B> match?' title — names ONLY the YES
    side, so it disambiguates the full title (which lists both teams). None if it doesn't match."""
    if not title:
        return None
    m = _WIN_TITLE.search(title)
    return m.group(1).strip() if m else None


def _tokens(s: str) -> list[str]:
    return [t for t in re.split(r"[^a-z0-9]+", (s or "").lower()) if t]


def pick_team_side(kalshi_side: str, outcomes: list[str]) -> int | None:
    """Conservative team pick: full-token overlap + 0.5 per prefix match
    ('Los Angeles D' -> 'Dodgers'). Requires a unique strict max >= 1; else None."""
    kt = _tokens(kalshi_side)
    if not kt or not outcomes:
        return None
    scores: list[float] = []
    for o in outcomes:
        ot = _tokens(o)
        full = sum(1 for t in kt if t in ot)
        prefix = sum(0.5 for t in kt if t not in ot and any(x.startswith(t) for x in ot))
        scores.append(full + prefix)
    best = max(scores)
    if best < 1 or scores.count(best) != 1:
        return None
    return scores.index(best)


def direction(text: str | None) -> str | None:
    """'over'/'under' parsed from a side/outcome string; None if neither is present."""
    s = (text or "").lower()
    if any(w in s for w in ("under", "below", "fewer", "less than", "or fewer", "or less")):
        return "under"
    if any(w in s for w in ("over", "above", "more than", "at least", "or more", "greater")):
        return "over"
    return None


def pick_total_side(kalshi_side: str, outcomes: list[str]) -> int | None:
    """Orient an over/under total by DIRECTION: the Kalshi YES side's over/under -> the PM
    outcome of the same direction. Conservative — explicit direction on the Kalshi side AND a
    unique PM outcome of that direction AND the opposite present; else None (never invert)."""
    kdir = direction(kalshi_side)
    if kdir is None:
        return None
    dirs = [direction(o) for o in outcomes]
    matches = [i for i, d in enumerate(dirs) if d == kdir]
    opp = "under" if kdir == "over" else "over"
    if len(matches) != 1 or opp not in dirs:
        return None
    return matches[0]


# spread proposition: team-named + a line. PM spread outcomes are [favorite, underdog] (favorite
# at index 0, verified on the box: ["Korea Republic","Mexico"] for "Spread: Korea Republic (-2.5)");
# the LINE lives in the PM title "(-X.5)", NOT in outcomes. The Kalshi YES ("<team> wins by more
# than/over N") is the FAVORITE covering -> PM outcomes[0]. Oriented by pick_spread_side.
SPREAD_BET_TYPES = frozenset({"spread"})

_SPREAD_KALSHI = re.compile(r"^(.+?)\s+wins?\s+by\s+(?:over|more\s+than)\s+([0-9]+(?:\.[0-9]+)?)",
                            re.IGNORECASE)
_SPREAD_PM_LINE = re.compile(r"\(\s*[-+]?\s*([0-9]+(?:\.[0-9]+)?)\s*\)")


def _parse_kalshi_spread(title: str | None) -> tuple[str | None, float | None]:
    """(yes_team, abs_line) from a Kalshi spread title 'Detroit wins by over 1.5 runs?' /
    'France wins by more than 1.5 goals?'. (None, None) if it isn't a spread-cover title."""
    m = _SPREAD_KALSHI.search(title or "")
    if not m:
        return None, None
    try:
        return m.group(1).strip(), abs(float(m.group(2)))
    except ValueError:
        return None, None


def _pm_spread_line(pm_title: str | None) -> float | None:
    """The abs line from a PM spread title 'Spread: Korea Republic (-2.5)'; None if absent."""
    m = _SPREAD_PM_LINE.search(pm_title or "")
    if not m:
        return None
    try:
        return abs(float(m.group(1)))
    except ValueError:
        return None


def pick_spread_side(kalshi_title: str | None, pm_title: str | None,
                     outcomes: list[str]) -> int | None:
    """Orient a spread: the Kalshi YES team (covering its line) maps to the PM FAVORITE outcome.
    PM spread outcomes are [favorite, underdog], so the favorite (Kalshi YES) is index 0 — but we
    VERIFY rather than assume: the line (from the PM title, not outcomes) must equal the Kalshi
    line, AND the Kalshi YES team must UNIQUELY token-match outcomes[0]. If it instead matches the
    underdog (idx 1) or is ambiguous, the pair is anomalous -> None (a wrong spread inverts the
    edge). Conservative throughout."""
    yes_team, k_line = _parse_kalshi_spread(kalshi_title)
    if not yes_team or k_line is None or not outcomes:
        return None
    p_line = _pm_spread_line(pm_title)
    if p_line is None or abs(k_line - p_line) > 1e-6:  # line must agree (matcher matched it; verify)
        return None
    # Kalshi YES = the favorite covering -> must be PM outcomes[0]. pick_team_side enforces a
    # unique match, so side==0 confirms YES-team==favorite (not the underdog, not ambiguous).
    return 0 if pick_team_side(yes_team, outcomes) == 0 else None


def orient_pair(
    bet_type: str | None,
    kalshi_title: str | None,
    pm_outcomes: list[str] | None,
    *,
    yes_subtitle: str | None = None,
    pm_title: str | None = None,
) -> tuple[int | None, str | None]:
    """(poly_side_index, poly_outcome) for a pair, or (None, None) if it can't be oriented
    unambiguously. team-WIN uses the title's YES team (fallback: yes_subtitle); totals use the
    yes_subtitle direction (the title has no direction); spreads verify team+line vs the PM title
    and map to the favorite (outcomes[0]). Conservative throughout."""
    if not pm_outcomes:
        return None, None
    if bet_type in TOTAL_BET_TYPES:
        side = pick_total_side(yes_subtitle or "", pm_outcomes)
    elif bet_type in TEAM_BET_TYPES:
        yes_name = yes_team_from_title(kalshi_title) or (yes_subtitle or "")
        side = pick_team_side(yes_name, pm_outcomes) if yes_name else None
    elif bet_type in SPREAD_BET_TYPES:
        side = pick_spread_side(kalshi_title, pm_title, pm_outcomes)
    else:
        return None, None
    if side is None or not (0 <= side < len(pm_outcomes)):
        return None, None
    return side, pm_outcomes[side]
