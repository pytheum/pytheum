"""Serve-safe request parsers, payload extractors, and pure-function helpers.

No embedding / rolling_index / PIT imports. Safe to import from pytheum-serve.
"""
from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from typing import Any


def resolution_horizon(
    resolution_at: Any, *, now: datetime | None = None
) -> tuple[float | None, bool]:
    """(days_to_resolution, is_stale) from a market's resolution_at.

    days_to_resolution: signed days until resolution (negative if in the past),
    rounded to 2dp; None when resolution_at is missing/unparseable.
    is_stale: True when resolution_at is already in the past — a market resolved
    or expired but still listed as `active`. This is the #1 discovery-noise
    source trader agents flagged (PSG/Hormuz/Iran markets resolving days ago but
    still surfaced as live)."""
    if resolution_at is None or resolution_at == "":
        return None, False
    if isinstance(resolution_at, datetime):
        d = resolution_at
    else:
        try:
            d = datetime.fromisoformat(str(resolution_at).replace("Z", "+00:00"))
        except ValueError:
            return None, False
    if d.tzinfo is None:
        d = d.replace(tzinfo=UTC)
    now = now or datetime.now(UTC)
    days = round((d - now).total_seconds() / 86400.0, 2)
    return days, d < now


def _norm_question(s: Any) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(s or "").lower()).strip()


def dedupe_markets_by_question(markets: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Collapse rows that are the same (venue, normalized question). Polymarket
    relists create duplicate market ids with identical question text (and often
    empty url), so both surface in discovery (e.g. 90178≡703258 'Jesus return',
    958443≡108634 'Iran regime fall'). Keep the richest row: prefer one with a
    `book`, then higher `volume_usd`. Order follows first appearance."""
    best: dict[Any, dict[str, Any]] = {}
    key_order: list[Any] = []
    for m in markets:
        q = _norm_question(m.get("question"))
        key: Any = (m.get("venue"), q) if q else ("__noq__", id(m))
        cur = best.get(key)
        if cur is None:
            best[key] = m
            key_order.append(key)
        else:
            rank_new = (m.get("book") is not None, m.get("volume_usd") or 0.0)
            rank_cur = (cur.get("book") is not None, cur.get("volume_usd") or 0.0)
            if rank_new > rank_cur:
                best[key] = m
    return [best[k] for k in key_order]


def parse_limit(query: dict[str, str], *, default: int, max_limit: int) -> int:
    raw = query.get("limit")
    if raw is None:
        return default
    try:
        v = int(raw)
    except ValueError:
        return default
    return max(1, min(v, max_limit))


def parse_min_similarity(query: dict[str, str], *, default: float) -> float:
    raw = query.get("min_similarity")
    if raw is None:
        return default
    try:
        v = float(raw)
    except ValueError:
        return default
    return max(0.0, min(v, 1.0))


def parse_kinds(query: dict[str, str]) -> set[str] | None:
    raw = query.get("kinds")
    if not raw:
        return None
    parts = {p.strip() for p in raw.split(",") if p.strip()}
    return parts or None


def parse_window_hours(query: dict[str, str], *, default: int = 24, max_hours: int = 168) -> int:
    """Parse the ?window_hours= param. Defaults to 24 (one day), caps at
    168 (one week) since that's the rolling-index storage window."""
    raw = query.get("window_hours")
    if raw is None:
        return default
    try:
        v = int(raw)
    except ValueError:
        return default
    return max(1, min(v, max_hours))


def parse_csv_list(raw: str | None) -> list[str] | None:
    if not raw:
        return None
    parts = [p.strip() for p in raw.split(",") if p.strip()]
    return parts or None


def market_event_key(row: dict[str, Any]) -> str:
    """Canonical 'event-level' identifier for collapsing outcome legs in top-K.

    Order of preference:
      1. Polymarket event slug parsed from `url` — slugs are 1:1 with the
         underlying event on Polymarket, so this is the strongest signal.
         Catches BOTH the outcome-leg dupes (iter 4 benchmark) AND the
         null-bundle-vs-bundled twin case (iter 1, task #156): one market
         in bundle `polymarket:fed` and a separate market with bundle_id=null
         can share the same URL slug → they collapse here.
      2. `bundle_id` if set — used when URL isn't a Polymarket event URL
         (Kalshi, Manifold, or a market without `url` populated).
      3. Falls back to `market_id` (no collapse).
    """
    url = row.get("url") or ""
    if "polymarket.com/event/" in url:
        slug = url.split("/event/", 1)[1].split("?", 1)[0].split("#", 1)[0]
        slug = slug.rstrip("/")
        if slug:
            return f"polymarket-event:{slug}"
    bundle = row.get("bundle_id")
    if bundle:
        return str(bundle)
    return str(row.get("market_id", ""))


def implied_yes_from_payload(payload: Any) -> float | None:
    """Best-effort YES implied probability from a market's stored `payload`.

    Polymarket listings carry `outcomePrices` (a JSON-string array like
    '["0.62", "0.38"]' where index 0 is YES). Today the markets sidecar does NOT
    persist this field (it writes only {competitive, comment_count, tags}), so
    this returns None — but the moment the sidecar starts persisting outcome
    prices (iter-38 follow-up in pytheum-core), sibling implied odds light up
    with zero further pytheum change. Fully defensive: any shape we don't
    recognize → None.
    """
    if payload is None:
        return None
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except (json.JSONDecodeError, ValueError):
            return None
    if not isinstance(payload, dict):
        return None
    raw = payload.get("outcomePrices") or payload.get("outcome_prices")
    if raw is None:
        return None
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            return None
    if not isinstance(raw, (list, tuple)) or not raw:
        return None
    try:
        p = float(raw[0])
    except (TypeError, ValueError):
        return None
    return p if 0.0 <= p <= 1.0 else None


def event_id_from_payload(payload: Any) -> str | None:
    """The parent event id a market belongs to, from its stored payload.

    Polymarket outcome legs carry `payload.event_id` == their parent event's
    numeric id (set by the ingest poly_row / the #204 event-children pass). Used
    to fetch a leg's structural co-legs (the other outcomes of the same event)
    — distinct from the fuzzy semantic sibling_markets. None when absent."""
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except (json.JSONDecodeError, ValueError):
            return None
    if not isinstance(payload, dict):
        return None
    eid = payload.get("event_id")
    return str(eid) if eid not in (None, "") else None


def book_from_payload(payload: Any) -> dict[str, float] | None:
    """Extract the top-of-book / tradeability block (G1) from a market payload.

    The price-refresh sidecar persists Gamma's `bestBid`/`bestAsk`/`spread`/
    `lastTradePrice`/`oneDayPriceChange` into the payload. A trader needs these
    to judge whether an edge survives the spread — pytheum is the substrate, not
    the fair value. Returns {bid, ask, spread, last, day_change} with the keys
    that are present (floats in [0,1] for prices; spread/day_change unclamped),
    or None when no book fields exist. Fully defensive on shape.
    """
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except (json.JSONDecodeError, ValueError):
            return None
    if not isinstance(payload, dict):
        return None

    def _f(key: str, *, clamp: bool = False) -> float | None:
        v = payload.get(key)
        if v is None:
            return None
        try:
            f = float(v)
        except (TypeError, ValueError):
            return None
        if clamp and not (0.0 <= f <= 1.0):
            return None
        return f

    book = {
        "bid": _f("bestBid", clamp=True),
        "ask": _f("bestAsk", clamp=True),
        "spread": _f("spread"),
        "last": _f("lastTradePrice", clamp=True),
        "day_change": _f("oneDayPriceChange"),
        # top-of-book resting size (contracts/shares) when the sidecar captured it
        # (Kalshi: bidSize/askSize from *_size_fp) — lets a trader gauge fillable
        # depth at the quote, not just the price. Unclamped (sizes, not prices).
        "bid_size": _f("bidSize"),
        "ask_size": _f("askSize"),
    }
    book_clean = {k: v for k, v in book.items() if v is not None}
    return book_clean or None


def condition_id_from_payload(payload: Any) -> str | None:
    """The Polymarket on-chain conditionId (0x…) from a market's stored payload
    (#238). Lets a trading agent verify a price against, or place an order on,
    the Polymarket CLOB directly without a separate lookup. None when absent
    (not yet re-synced, or non-Polymarket)."""
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except (json.JSONDecodeError, ValueError):
            return None
    if not isinstance(payload, dict):
        return None
    cid = payload.get("conditionId")
    return cid if isinstance(cid, str) and cid else None


def resolution_status_from_payload(payload: Any) -> str | None:
    """The market's UMA resolution state, from its stored payload (#225).

    Polymarket's Gamma `umaResolutionStatus` is set when a resolution is in
    flight: `"proposed"` (a resolution proposed, in the challenge window — price
    is pinning to 0/1) or `"disputed"` (contested, a UMA vote is underway — the
    quoted price reflects ORACLE-PROCESS uncertainty, NOT the real-world
    probability). A trader probe twice mistook a disputed market for a live edge
    (MSTR-sold-BTC frozen at ~0.75% during a UMA dispute). Surfacing this lets an
    agent know not to read a disputed/proposed price as a probability. None for
    normally-trading markets (the field is absent/empty)."""
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except (json.JSONDecodeError, ValueError):
            return None
    if not isinstance(payload, dict):
        return None
    status = payload.get("uma_resolution_status")
    if not isinstance(status, str):
        return None
    status = status.strip().lower()
    return status or None


_MAX_RESOLUTION_CHARS = 8000


def resolution_from_payload(payload: Any) -> str | None:
    """The market's resolution criteria text, from its stored payload.

    iter-44: the lift benchmark kept losing on "resolution mechanics" and pytheum
    MISFRAMED them (Anthropic judged "subjective" when it resolves on LMArena
    rank; Venezuela "missing the de jure resolution rule"). Polymarket's listing
    `description` IS the resolution rule ("This market will resolve to…"); the
    markets sidecar + price-refresh now persist it (pytheum-core poly_row). Web
    must hunt for this — pytheum has it structured. Truncated; None when absent
    (e.g. Kalshi listings that don't carry rules in payload — separate follow-up).
    """
    if payload is None:
        return None
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except (json.JSONDecodeError, ValueError):
            return None
    if not isinstance(payload, dict):
        return None
    desc = payload.get("description")
    if not isinstance(desc, str):
        return None
    desc = desc.strip()
    if not desc:
        return None
    return desc[:_MAX_RESOLUTION_CHARS] + ("…" if len(desc) > _MAX_RESOLUTION_CHARS else "")


_LADDER_NUM = re.compile(r"\$?\s*([0-9][0-9,]*(?:\.[0-9]+)?)([kKmMbB])?(?![A-Za-z])")
_LADDER_UP = ("≥", ">=", ">", "above", "over", "at least", "reach", "more", "higher", "greater")
_LADDER_DOWN = ("≤", "<=", "<", "below", "under", "at most", "less", "lower", "fewer")


def _ladder_threshold(label: str) -> float | None:
    """Parse the strike from a threshold-leg label. Suffix (k/m/b) must be
    attached to the digits and not followed by a letter — else "$180,000 by
    Dec" parsed the 'b' in 'by' as billions (validated 2026-06-02)."""
    if not label:
        return None
    m = _LADDER_NUM.search(label)
    if not m:
        return None
    n = float(m.group(1).replace(",", ""))
    return n * {"k": 1e3, "m": 1e6, "b": 1e9}.get((m.group(2) or "").lower(), 1)


def _ladder_direction(label: str) -> str | None:
    low = (label or "").lower()
    if "↓" in label or any(k in low for k in _LADDER_DOWN):
        return "down"
    if "↑" in label or any(k in low for k in _LADDER_UP):
        return "up"
    return None


def ladder_monotonicity(legs: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Flag CDF violations in a cumulative-threshold bundle (#242). For a single
    real-world variable, P(>X) must DECREASE as X rises ('up' legs) and P(<X)
    must INCREASE ('down' legs); an adjacent pair that breaks this is a logical
    arb / data error (the crypto probe found ↑190k priced ABOVE ↑180k). Handles
    two-sided ladders (Polymarket's ↑/↓ legs interleaved) by checking each
    direction's sub-ladder. Returns None for non-threshold bundles (categorical
    fields like "which team wins" never parse to a consistent strike ladder), so
    it never false-flags. Needs ≥3 distinct strikes in a direction to assert."""
    groups: dict[str, list[tuple[float, float, str]]] = {"up": [], "down": []}
    for o in legs or []:
        label = o.get("outcome") or o.get("question") or ""
        t = _ladder_threshold(label)
        d = _ladder_direction(label)
        iy = o.get("implied_yes")
        if t is not None and d and isinstance(iy, (int, float)):
            groups[d].append((t, iy, label.strip()))
    inversions: list[dict[str, Any]] = []
    checked = False
    for d, ld in groups.items():
        if len({t for t, _, _ in ld}) < 3:
            continue
        checked = True
        ld.sort(key=lambda x: x[0])
        for (_t1, p1, l1), (_t2, p2, l2) in zip(ld, ld[1:], strict=False):
            if (d == "up" and p2 > p1 + 1e-9) or (d == "down" and p2 < p1 - 1e-9):
                inversions.append({"dir": d, "lower_strike": l1, "p": p1,
                                   "higher_strike": l2, "p2": p2})
    if not checked:
        return None
    return {"monotonic": len(inversions) == 0, "n_inversions": len(inversions),
            "inversions": inversions[:6]}


_RES_YEAR = re.compile(r"\b(20[2-3]\d)\b")
# Resolver authorities a market's rules cite — first match wins, canonical label.
# Lets an agent confirm two cross-venue markets resolve off the SAME source
# (#219; enabler for safe auto-netting #247 — the Recession pair both cite BEA).
_RES_SOURCES: tuple[tuple[tuple[str, ...], str], ...] = (
    (("bureau of economic analysis", "bea"), "BEA"),
    (("national bureau of economic research", "nber"), "NBER"),
    (("bureau of labor statistics", "bls"), "BLS"),
    (("federal reserve", "fomc", "federal open market"), "Federal Reserve"),
    (("lmarena", "arena.ai", "chatbot arena"), "LMArena"),
    (("coingecko",), "CoinGecko"),
    (("binance",), "Binance"),
    (("coinbase",), "Coinbase"),
    (("census bureau", "u.s. census"), "Census"),
    (("federal election commission",), "FEC"),
    (("associated press",), "AP"),
)


def resolution_window_years(text: str | None) -> list[int] | None:
    """4-digit resolution years (2024–2031) referenced in the rules prose, sorted.
    A cheap structured signal for cross-venue equivalence: two markets whose
    resolution windows differ (e.g. one says 2025-2026, the other 2026 only) may
    not be the same bet even when the questions match (#219/#247)."""
    if not text:
        return None
    ys = sorted({int(y) for y in _RES_YEAR.findall(text) if 2024 <= int(y) <= 2031})
    return ys or None


def resolution_source(text: str | None) -> str | None:
    """Canonical resolver authority cited in the rules (BEA/NBER/LMArena/...),
    or None if none recognized. Same-source is a strong equivalence signal."""
    if not text:
        return None
    low = text.lower()
    for keys, label in _RES_SOURCES:
        if any(k in low for k in keys):
            return label
    return None


def build_outcome_ladder(
    rows: list[dict[str, Any]],
    *,
    limit: int = 20,
) -> list[dict[str, Any]]:
    """Assemble the per-outcome price ladder for a multi-outcome / event market
    from its child rows. Each event-parent (e.g. "World Cup Winner", "Next French
    Presidential Election") shows implied_yes=None because the price lives on
    per-outcome children — this turns those children into a ranked ladder
    [{outcome, market_id, implied_yes, book, volume_usd}] so a trader sees
    Spain 17% / France 12% / ... in the one call instead of a null.
    Sorted by implied_yes desc; rows without a price are dropped (untradeable /
    not-yet-refreshed); deduped by outcome label.
    """
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for r in rows:
        iy = implied_yes_from_payload(r.get("payload"))
        if iy is None:
            continue
        pay = r.get("payload")
        if isinstance(pay, str):
            try:
                pay = json.loads(pay)
            except (json.JSONDecodeError, ValueError):
                pay = {}
        label = (pay.get("group_item_title") if isinstance(pay, dict) else None)
        label = (label or r.get("question") or "").strip()
        key = label.lower()
        if key and key in seen:
            continue
        if key:
            seen.add(key)
        out.append({
            "outcome": label or None,
            "market_id": r.get("id"),
            "implied_yes": iy,
            "book": book_from_payload(r.get("payload")),
            "volume_usd": r.get("volume_usd"),
            "condition_id": condition_id_from_payload(r.get("payload")),
            "flow_flag": flow_flag_from_row(r),
        })
    out.sort(key=lambda o: o["implied_yes"] or 0.0, reverse=True)
    return out[:limit]


_FLOW_FLAG_STALE_S = 3 * 3600


def flow_flag_from_row(row: dict[str, Any]) -> str | None:
    """The null-by-default flow breadcrumb. Reads the LEFT-JOINed
    market_flow_signal.flow_flag column; null when no row / below threshold / the
    stored signal is stale (>_FLOW_FLAG_STALE_S — sidecar parked, #223)."""
    raw = row.get("flow_flag")
    if not raw:
        return None
    flag = str(raw)
    ts = row.get("flow_flag_updated_at")
    if ts is not None:
        try:
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=UTC)
            if (datetime.now(UTC) - ts).total_seconds() > _FLOW_FLAG_STALE_S:
                return None  # stale precomputed flag — don't assert positioning
        except (AttributeError, TypeError):
            pass  # unparseable timestamp → fall through and serve the flag
    return flag
