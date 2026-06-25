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


def orient_pair(
    bet_type: str | None,
    kalshi_title: str | None,
    pm_outcomes: list[str] | None,
    *,
    yes_subtitle: str | None = None,
) -> tuple[int | None, str | None]:
    """(poly_side_index, poly_outcome) for a pair, or (None, None) if it can't be oriented
    unambiguously. team-WIN uses the title's YES team (fallback: yes_subtitle); totals use the
    yes_subtitle direction (the title has no direction). Conservative throughout."""
    if not pm_outcomes:
        return None, None
    if bet_type in TOTAL_BET_TYPES:
        side = pick_total_side(yes_subtitle or "", pm_outcomes)
    elif bet_type in TEAM_BET_TYPES:
        yes_name = yes_team_from_title(kalshi_title) or (yes_subtitle or "")
        side = pick_team_side(yes_name, pm_outcomes) if yes_name else None
    else:
        return None, None
    if side is None or not (0 <= side < len(pm_outcomes)):
        return None, None
    return side, pm_outcomes[side]
