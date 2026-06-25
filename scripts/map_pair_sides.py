"""Map which side of a Polymarket game market matches each Kalshi leg.

Game/tennis/esports moneyline pairs in the matcher gold set link MARKET to
MARKET, but a Polymarket game market quotes its FIRST-LISTED outcome while the
Kalshi leg's YES side is whatever team the ticker names — verified opposite on
a live tennis pair (Kalshi 'Will Moutet win' vs poly 'Kyrgios vs Moutet'). The
divergence scanner refuses to edge-score those pairs until the side is known.

This maps sides from two PUBLIC sources: Kalshi's `yes_sub_title` (names the
YES side; /trade-api/v2/markets is unauthenticated for metadata) and Gamma's
`outcomes` array (full team/player names, persisted into payload by
scripts/sync_paired_polymarket). Matching is conservative token overlap with a
prefix tiebreak (Kalshi truncates: 'Los Angeles D' -> 'Los Angeles Dodgers');
ambiguity -> unmapped, never guessed. Hits land in `pair_side_map`, which
survives equivalence reloads.

Usage:
    python -m scripts.map_pair_sides [--all] [--limit N]
"""
from __future__ import annotations

import argparse
import asyncio
import json
import re
from typing import Any

import asyncpg
import httpx

from scripts.load_market_equivalence import _db_url

_KALSHI = "https://api.elections.kalshi.com/trade-api/v2/markets"
_GAMMA = "https://gamma-api.polymarket.com/markets/"
_BATCH = 20

_DDL = """
CREATE TABLE IF NOT EXISTS pair_side_map (
    kalshi_market_id     text NOT NULL,
    polymarket_market_id text NOT NULL,
    poly_side            smallint NOT NULL,
    poly_outcome         text,
    kalshi_side_name     text,
    side_method          text,
    mapped_at            timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (kalshi_market_id, polymarket_market_id)
);
"""

_TARGETS = """
SELECT e.kalshi_market_id, e.polymarket_market_id, e.bet_type, pa.payload
FROM market_equivalence e
JOIN markets ka ON ka.id = e.kalshi_market_id {active_clause}
JOIN markets pa ON pa.id = e.polymarket_market_id
LEFT JOIN pair_side_map ps
       ON ps.kalshi_market_id = e.kalshi_market_id
      AND ps.polymarket_market_id = e.polymarket_market_id
WHERE e.polymarket_market_id IS NOT NULL
  AND e.bet_type = ANY($1::text[])
  AND ps.kalshi_market_id IS NULL
"""

# Team/player-token mappable: the proposition is "this side WINS outright", so
# team-token overlap between Kalshi's yes_sub_title and Gamma's outcomes is sound.
_MAPPABLE_BET_TYPES = [
    "moneyline", "moneyline_outcome", "tennis_ml", "esports_series", "esports_map",
]

# Over/Under (total) families: the proposition is directional, not team-named — the
# Kalshi total YES side is "over/under the threshold" and PM lists explicit Over/Under
# outcomes (line already matched by the matcher). Oriented by DIRECTION via pick_total_side
# (the proposition-aware mapper the old comment said was needed). Spreads stay out — they're
# directional AND team-named (a proposition-aware spread mapper is a further step).
_TOTAL_BET_TYPES = [
    "total", "total_1h", "team_total", "tennis_total", "esports_total", "wc_2h_total",
]


def _direction(text: str) -> str | None:
    """'over'/'under' parsed from a side/outcome string; None if neither is present."""
    s = (text or "").lower()
    if any(w in s for w in ("under", "below", "fewer", "less than", "or fewer", "or less")):
        return "under"
    if any(w in s for w in ("over", "above", "more than", "at least", "or more", "greater")):
        return "over"
    return None


def pick_total_side(kalshi_side: str, outcomes: list[str]) -> int | None:
    """Orient an over/under total: the Kalshi YES side's direction maps to the PM outcome
    of the SAME direction. Conservative — requires an explicit direction on the Kalshi side
    AND a unique PM outcome of that direction AND the opposite PM outcome present; otherwise
    None (never assume — a wrong total orientation INVERTS the edge, worse than unmapped)."""
    kdir = _direction(kalshi_side)
    if kdir is None:
        return None
    dirs = [_direction(o) for o in outcomes]
    matches = [i for i, d in enumerate(dirs) if d == kdir]
    opp = "under" if kdir == "over" else "over"
    if len(matches) != 1 or opp not in dirs:
        return None
    return matches[0]


def _tokens(s: str) -> list[str]:
    return [t for t in re.split(r"[^a-z0-9]+", (s or "").lower()) if t]


def pick_side(kalshi_side: str, outcomes: list[str]) -> int | None:
    """Conservative side pick: full-token overlap + 0.5 per prefix match
    ('Los Angeles D' -> 'Dodgers'). Requires a unique strict max >= 1."""
    kt = _tokens(kalshi_side)
    if not kt or not outcomes:
        return None
    scores: list[float] = []
    for o in outcomes:
        ot = _tokens(o)
        full = sum(1 for t in kt if t in ot)
        prefix = sum(0.5 for t in kt
                     if t not in ot and any(x.startswith(t) for x in ot))
        scores.append(full + prefix)
    best = max(scores)
    if best < 1 or scores.count(best) != 1:
        return None
    return scores.index(best)


def _outcomes_from_payload(payload: Any) -> list[str] | None:
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


async def run(*, scope_all: bool, limit: int | None) -> None:
    con = await asyncpg.connect(_db_url(), statement_cache_size=0)
    try:
        await con.execute("SET statement_timeout = 0")
        await con.execute(_DDL)
        q = _TARGETS.format(active_clause="" if scope_all else "AND ka.status = 'active'")
        if limit:
            q += f" LIMIT {int(limit)}"
        targets = await con.fetch(q, _MAPPABLE_BET_TYPES + _TOTAL_BET_TYPES)
        print(f"mapping sides for {len(targets)} pairs")

        # Poly outcomes — payload first, Gamma fallback for pre-existing rows.
        outcomes: dict[str, list[str]] = {}
        need_gamma: list[str] = []
        for t in targets:
            o = _outcomes_from_payload(t["payload"])
            if o:
                outcomes[t["polymarket_market_id"]] = o
            else:
                need_gamma.append(t["polymarket_market_id"])
        async with httpx.AsyncClient() as client:
            sem = asyncio.Semaphore(4)

            async def fetch_outcomes(pid: str) -> None:
                async with sem:
                    try:
                        r = await client.get(_GAMMA + pid.split(":", 1)[1], timeout=15)
                        r.raise_for_status()
                        o = _outcomes_from_payload({"outcomes": r.json().get("outcomes")})
                        if o:
                            outcomes[pid] = o
                    except (httpx.HTTPError, ValueError):
                        pass
                    await asyncio.sleep(0.1)

            for i in range(0, len(need_gamma), 500):
                await asyncio.gather(*(fetch_outcomes(p) for p in need_gamma[i:i + 500]))

            # Kalshi yes_sub_title in batches.
            side_names: dict[str, str] = {}
            tickers = sorted({t["kalshi_market_id"].split(":", 1)[1] for t in targets})
            for i in range(0, len(tickers), _BATCH):
                chunk = tickers[i:i + _BATCH]
                try:
                    r = await client.get(_KALSHI,
                                         params={"tickers": ",".join(chunk)}, timeout=20)
                    r.raise_for_status()
                    for m in r.json().get("markets") or []:
                        if m.get("ticker") and m.get("yes_sub_title"):
                            side_names["kalshi:" + m["ticker"]] = m["yes_sub_title"]
                except (httpx.HTTPError, ValueError):
                    pass
                await asyncio.sleep(0.1)
                if i and i % 2000 == 0:
                    print(f"  kalshi side names: {i}/{len(tickers)}")

        rows: list[tuple] = []
        ambiguous = 0
        missing = 0
        for t in targets:
            kid, pid = t["kalshi_market_id"], t["polymarket_market_id"]
            name = side_names.get(kid)
            outs = outcomes.get(pid)
            if not name or not outs:
                missing += 1
                continue
            is_total = t["bet_type"] in _TOTAL_BET_TYPES
            side = pick_total_side(name, outs) if is_total else pick_side(name, outs)
            if side is None:
                ambiguous += 1
                continue
            method = "total_overunder" if is_total else "token_subtitle"
            rows.append((kid, pid, side, outs[side], name, method))
        if rows:
            await con.executemany(
                "INSERT INTO pair_side_map (kalshi_market_id, polymarket_market_id, "
                " poly_side, poly_outcome, kalshi_side_name, side_method) "
                "VALUES ($1, $2, $3, $4, $5, $6) "
                "ON CONFLICT (kalshi_market_id, polymarket_market_id) DO NOTHING",
                rows,
            )
        print(f"mapped={len(rows)} ambiguous={ambiguous} missing_inputs={missing}")
    finally:
        await con.close()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--all", action="store_true", help="include settled kalshi legs")
    ap.add_argument("--limit", type=int, default=None)
    args = ap.parse_args()
    asyncio.run(run(scope_all=args.all, limit=args.limit))


if __name__ == "__main__":
    main()
