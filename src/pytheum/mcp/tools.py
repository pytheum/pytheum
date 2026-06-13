"""Thin wrappers around the REST endpoints. Each function is one tool."""
from __future__ import annotations

import contextlib
import os
import re
import time
from typing import Any
from urllib.parse import quote, urlencode

import httpx

DEFAULT_BASE = os.environ.get("PYTHEUM_API_BASE", "https://api.pytheum.com")


class _ApiError(Exception):
    """An HTTP error from the REST layer, carrying status + a clean detail string.

    Raised instead of httpx.HTTPStatusError so the internal URL (http://127.0.0.1
    :8443/...) NEVER leaks to the agent — the adversarial robustness probe found
    every per-market tool was echoing that URL on any bad ref (2026-06-04)."""

    def __init__(self, status: int, detail: str = "") -> None:
        self.status = status
        self.detail = detail
        super().__init__(f"upstream {status}")


async def _get(path: str, params: dict[str, Any], base_url: str) -> dict[str, Any]:
    qs = urlencode({k: v for k, v in params.items() if v is not None})
    url = f"{base_url}{path}" + (f"?{qs}" if qs else "")
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.get(url)
        if resp.is_error:
            detail = ""
            with contextlib.suppress(Exception):  # body may be non-JSON
                detail = (resp.json() or {}).get("detail") or ""
            raise _ApiError(resp.status_code, str(detail))
        return resp.json()


# --- agent-robustness helpers (informative failures, no internal-URL leak) ----
_VENUE_PREFIXES = ("kalshi:", "polymarket:", "manifold:")
_REF_EXAMPLES = "e.g. 'kalshi:KXNBA-26-NYK' or 'polymarket:558936', or a full market URL"
_COMMON_STATUSES = {"active", "resolved", "closed"}


def _short(v: Any, n: int = 120) -> str:
    """Truncate an echoed user value so a 2000–8000 char bad ref/query doesn't
    bloat the error response (probe r4)."""
    s = v if isinstance(v, str) else str(v)
    return s if len(s) <= n else s[:n] + f"…(+{len(s) - n} chars)"


def _market_ref_error(ref: Any, *, param: str = "market_ref") -> dict[str, Any] | None:
    """Catch the two most common agent mistakes BEFORE the HTTP call: an empty ref,
    and a ref missing its venue prefix (the probe's #1 failure — agents pass
    'KXNBA-26-NYK' / '558936' bare). Returns an informative error dict, else None.
    URLs and venue-prefixed ids pass through (resolved server-side)."""
    if not isinstance(ref, str) or not ref.strip():
        return {"error": "invalid_market_ref",
                "hint": f"{param} is required and must be a non-empty string — {_REF_EXAMPLES}."}
    low = ref.strip().lower()
    if low.startswith(("http://", "https://")) or low.startswith(_VENUE_PREFIXES):
        return None
    return {"error": "missing_venue_prefix", param: _short(ref),
            "hint": (f"{param} must be venue-prefixed — {_REF_EXAMPLES}. "
                     f"You passed '{_short(ref, 80)}' with no 'kalshi:'/'polymarket:'/'manifold:' "
                     "prefix. Discover ids with t_screen or t_find_markets.")}


async def _get_market(path: str, params: dict[str, Any], base_url: str, *,
                      ref: str, kind: str = "market", bundle_hint: bool = False) -> dict[str, Any]:
    """_get for per-id lookups: turns a 404/upstream error into an informative dict
    (never the raw httpx URL). On 404, optionally adds the bundle-parent hint."""
    try:
        return await _get(path, params, base_url)
    except _ApiError as e:
        if e.status == 404:
            hint = (f"No {kind} '{_short(ref, 80)}'. Check it's venue-prefixed ({_REF_EXAMPLES}) "
                    "and still listed.")
            if bundle_hint:
                hint += (" If this id is a bundle/event PARENT it has no own data — query an outcome "
                         "leg instead (e.g. the '…-NYK' / numeric child), or list legs via t_screen.")
            return {"error": f"{kind}_not_found", "ref": _short(ref), "hint": hint}
        return {"error": "upstream_error", "status": e.status,
                "detail": (e.detail or "")[:200] or f"the API returned HTTP {e.status}"}


_VENUE_ALIASES = {"poly": "polymarket", "polymarket": "polymarket",
                  "kalshi": "kalshi", "manifold": "manifold", "mani": "manifold"}
_ALL_VENUE_WORDS = {"all", "both", "any", "*", "every"}


def _normalize_venues(venues: str | list[str] | None) -> tuple[str | None, dict[str, Any] | None]:
    """Normalize a venue arg into (comma-string|None, error|None). Case-folds, maps
    aliases (poly→polymarket), treats 'all'/'both'/'any' as "omit = all venues", and
    REJECTS unknown tokens with a clear error instead of the old silent-empty result
    (probe: 'both'/'all'/'Kalshi'/'binance' all returned count:0 misleadingly)."""
    if venues is None:
        return None, None
    toks = ([t.strip() for t in venues.split(",")] if isinstance(venues, str)
            else [str(t).strip() for t in venues])
    toks = [t for t in toks if t]
    if not toks:
        return None, None
    out: list[str] = []
    for t in toks:
        low = t.lower()
        if low in _ALL_VENUE_WORDS:
            return None, None  # all venues → omit the filter
        mapped = _VENUE_ALIASES.get(low)
        if mapped is None:
            return None, {"error": "unknown_venue", "value": t,
                          "hint": ("venues must be one or more of kalshi, polymarket, manifold "
                                   "(case-insensitive, comma-separated); omit for all venues. "
                                   f"Unrecognized: '{t}'.")}
        if mapped not in out:
            out.append(mapped)
    return ",".join(out), None


def _date_error(val: Any, param: str) -> dict[str, Any] | None:
    """Reject an unparseable date instead of silently dropping the filter (the
    probe's dangerous case: resolves_before='not-a-date' returned the UNfiltered
    universe, so an agent thinks it filtered by date when it didn't)."""
    if val is None or val == "":
        return None
    try:
        from datetime import datetime
        datetime.fromisoformat(str(val).replace("Z", "+00:00"))
        return None
    except (ValueError, TypeError):
        return {"error": "invalid_date", param: val,
                "hint": f"{param} must be ISO-8601 (e.g. '2026-12-31' or '2026-12-31T00:00:00Z'). "
                        f"Got '{val}'."}


def _normalize_market_ref(ref: Any) -> Any:
    """Trim whitespace and case-fold a venue prefix so '  KALSHI:KXNBA-26-NYK  '
    and 'Polymarket:558936' resolve like the canonical form (probe r2: agents
    pass wrong-case/whitespace refs and got market_not_found). Non-strings and
    URLs pass through untouched."""
    if not isinstance(ref, str):
        return ref
    r = ref.strip()
    head, sep, rest = r.partition(":")
    if sep and head.strip().lower() in ("kalshi", "polymarket", "manifold"):
        # strip whitespace around the id body too — 'POLYMARKET: 558936' must
        # resolve like 'polymarket:558936', not 404 as 'polymarket: 558936'
        # (probe iter9: a stray space after the colon silently not_found'd a
        # market that exists).
        return f"{head.strip().lower()}:{rest.strip()}"
    return r


def _range_error(val: Any, param: str, lo: float, hi: float) -> dict[str, Any] | None:
    """Reject a numeric param outside [lo, hi] instead of silently clamping to an
    empty/misleading result (probe r2: min_similarity=2.0 → empty, =-1 → full)."""
    if val is None:
        return None
    if not isinstance(val, (int, float)) or isinstance(val, bool) or not (lo <= val <= hi):
        return {"error": "out_of_range", param: val,
                "hint": f"{param} must be a number in [{lo}, {hi}]. Got {val!r}."}
    return None


def _limit_error(val: Any) -> dict[str, Any] | None:
    """Reject a non-positive/float limit consistently across tools (probe r5:
    t_screen accepted limit=-3 silently while history/divergences errored)."""
    if not isinstance(val, int) or isinstance(val, bool) or val < 1:
        return {"error": "invalid_limit", "limit": val,
                "hint": "limit must be an integer >= 1."}
    return None


def _enum_error(val: Any, param: str, allowed: set[str]) -> dict[str, Any] | None:
    """Reject an unknown enum value instead of silently ignoring it (probe r2:
    sort_by='price' silently fell back to volume → a silently-WRONG ordering)."""
    if val is None:
        return None
    if not isinstance(val, str) or val.lower() not in allowed:
        return {"error": f"invalid_{param}", param: val,
                "hint": f"{param} must be one of {sorted(allowed)}. Got {val!r}."}
    return None


# Real-money vs play-money: Manifold settles in mana (play money), so its prices
# are a soft signal and "cross-venue edges" against it are NOT capitalizable. Every
# trader probe (2026-06-02) chased Manifold divergence by mistake. Tag each row so
# an agent can filter. Kalshi + Polymarket are real-money.
_PLAY_MONEY_VENUES = {"manifold"}


def _coerce_venues(venues: str | list[str] | None) -> str | None:
    """venues may arrive as a single string ("kalshi"), a comma-list, or a list —
    normalize to the comma-string the REST endpoints expect. (Agents naturally
    pass the bare string; ",".join on a str would mangle it to "k,a,l,s,h,i".)"""
    if isinstance(venues, str):
        return venues
    if venues:
        return ",".join(venues)
    return None


def _row_fee_bps(venue: Any, implied_yes: Any) -> float | None:
    """Approximate taker fee in bps of the $1 notional, so an agent can turn a
    GROSS cross-venue edge into a NET one (every trader probe quoted gross edges
    and flagged they're optimistic without fees). Polymarket charges no trading
    fee today (gas only) -> 0. Kalshi's general schedule is ~0.07*p*(1-p) per
    contract -> 700*p*(1-p) bps of the $1 notional (price-dependent; some series
    differ, so this is an estimate). Manifold is play-money -> None."""
    if venue == "polymarket":
        return 0.0
    if venue == "kalshi" and isinstance(implied_yes, (int, float)) and 0 < implied_yes < 1:
        return round(700 * implied_yes * (1 - implied_yes), 1)
    return None


def _fee_dollars(venue: Any, price: Any) -> float | None:
    """Taker fee in DOLLARS per $1-notional contract AT a given execution price.
    Polymarket: 0 (no trading fee). Kalshi: 0.07*p*(1-p) general schedule.
    Manifold/unknown: None (play money)."""
    if venue == "polymarket":
        return 0.0
    if venue == "kalshi" and isinstance(price, (int, float)) and 0 < price < 1:
        return 0.07 * price * (1 - price)
    return None


def _net_book(venue: Any, book: Any) -> None:
    """In-place: add fee-adjusted all-in prices to a book so an agent reads net
    edge instead of recomputing it (trader wishlist #1). yes_ask_net = cost to
    BUY yes (ask + fee@ask); no_ask_net = cost to BUY no (1-bid + fee); yes_bid_net
    = proceeds SELLING yes (bid - fee@bid). Omitted when fee is unknown (Manifold)
    or the side is absent. For Polymarket fee=0 so net == raw (still emitted, so
    cross-venue net comparison is a uniform lookup)."""
    if not isinstance(book, dict):
        return
    ask, bid = book.get("ask"), book.get("bid")
    if isinstance(ask, (int, float)):
        f = _fee_dollars(venue, ask)
        if f is not None:
            book["yes_ask_net"] = round(ask + f, 4)
    if isinstance(bid, (int, float)):
        f = _fee_dollars(venue, bid)
        if f is not None:
            book["yes_bid_net"] = round(bid - f, 4)
        no_ask = 1 - bid
        f2 = _fee_dollars(venue, no_ask)
        if f2 is not None:
            book["no_ask_net"] = round(no_ask + f2, 4)


# Each venue reports "volume" in a different unit, so raw volume_usd is NOT
# cross-venue comparable (every trader probe asked for one labeled axis):
#   polymarket -> traded USDC (already USD)
#   kalshi     -> traded CONTRACT COUNT (each settles 0..$1)
#   manifold   -> MANA (play money, no USD value)
_VOLUME_UNIT = {"polymarket": "usd", "kalshi": "contracts", "manifold": "mana"}


def _volume_usd_norm(venue: Any, volume_usd: Any, implied_yes: Any) -> float | None:
    """Best-effort USD-comparable volume so an agent can rank liquidity on ONE
    axis. Polymarket is already USD. Kalshi's volume is contracts; match
    Polymarket's shares*price convention with contracts*price (price≈implied_yes,
    0.5 fallback) — an ESTIMATE, not exact (no avg-execution-price available).
    Manifold is play-money mana -> None (not USD-comparable)."""
    if venue == "polymarket":
        return volume_usd if isinstance(volume_usd, (int, float)) else None
    if venue == "kalshi" and isinstance(volume_usd, (int, float)):
        p = implied_yes if isinstance(implied_yes, (int, float)) and 0 < implied_yes < 1 else 0.5
        return round(volume_usd * p, 2)
    return None  # manifold mana / unknown


# High-precision NOVELTY patterns: cosmic / supernatural / joke markets that
# trade at a persistent premium over ~0 (trader wishlist #5 — the carry basket).
# Deliberately narrow to avoid mislabeling real markets: "aliens" only counts
# with a cosmic qualifier (NOT bare "alien", which is immigration).
_NOVELTY_RE = re.compile(
    r"\b(extraterrestrial|ufos?|uap|alien life|aliens? exist|confirm[a-z ]*aliens?|"
    r"second coming|jesus|rapture|messiah|antichrist|"
    r"in a simulation|simulation hypothesis|bigfoot|loch ness|time travel|zombie|"
    r"end of the world|world (?:will )?end|apocalypse|raptured)\b", re.I)
# Ordered domain keywords (first match wins) for the non-novelty archetype.
_ARCHETYPE_KEYWORDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("crypto", ("bitcoin", "btc", "ethereum", " eth ", "solana", " sol ", "crypto",
                "dogecoin", "xrp", "stablecoin")),
    ("macro", ("fed ", "fomc", "interest rate", "rate cut", "rate hike", "inflation",
               " cpi", " gdp", "recession", "unemployment", "jobless", "jobs report",
               "powell", "fed funds")),
    ("tech_ai", ("gpt", "openai", "anthropic", "claude", " llm", " agi", "ai model",
                 "lmarena", "nvidia", "frontier model", "benchmark")),
    ("politics", ("election", "nominee", "president", "senate", "the house", "governor",
                  "primary", "midterm", "parliament", "prime minister", "control of",
                  "balance of power")),
    ("sports", ("nba", "nfl", "mlb", "nhl", "super bowl", "world cup", "finals",
                "playoff", "champion", "premier league", "uefa", "grand slam",
                "french open", "wimbledon", "world series", "stanley cup")),
    ("geopolitics", ("ceasefire", "invade", "invasion", "nuclear", "iran", "ukraine",
                     "russia", "israel", "gaza", "taiwan", "war ", "peace deal",
                     "regime")),
    ("weather", ("hurricane", "temperature", "earthquake", "tropical storm", "snowfall")),
)


def _market_archetype(question: Any) -> str | None:
    """Coarse, deterministic market type so an agent can filter (e.g. drop the
    novelty_longshot carry basket in one pass instead of regexing titles).
    Heuristic over the question text — 'novelty_longshot' is high-precision
    cosmic/joke; otherwise a domain bucket; None if no text."""
    if not isinstance(question, str) or not question.strip():
        return None
    if _NOVELTY_RE.search(question):
        return "novelty_longshot"
    low = " " + question.lower() + " "
    for archetype, kws in _ARCHETYPE_KEYWORDS:
        if any(k in low for k in kws):
            return archetype
    return "other"


def _enrich_row(r: Any) -> None:
    """In-place: tag a market row with is_play_money + taker_fee_bps +
    volume_unit + volume_usd_norm (one cross-venue-comparable axis) +
    market_archetype."""
    if isinstance(r, dict) and "venue" in r:
        v = r.get("venue")
        r["is_play_money"] = v in _PLAY_MONEY_VENUES
        r["taker_fee_bps"] = _row_fee_bps(v, r.get("implied_yes"))
        r["volume_unit"] = _VOLUME_UNIT.get(v)
        r["volume_usd_norm"] = _volume_usd_norm(v, r.get("volume_usd"), r.get("implied_yes"))
        _net_book(v, r.get("book"))  # fee-adjusted all-in prices into the book (#250)
        # …and on every priced bundle-outcome LEG too — a trader probe had to
        # recompute 1-bid by hand on the World-Cup/NBA ladder legs because only
        # the parent row carried net prices (#237). Legs inherit the parent venue.
        for _leg in (r.get("bundle_outcomes") or []):
            if isinstance(_leg, dict):
                _net_book(v, _leg.get("book"))
        r["market_archetype"] = _market_archetype(r.get("question"))  # #252


def _annotate_play_money(payload: Any) -> Any:
    """Enrich every market row in a screen/find response (is_play_money +
    taker_fee_bps), keyed off venue/price. In-place; returns payload for chaining."""
    if isinstance(payload, dict):
        rows = payload.get("markets") or payload.get("results")
        if isinstance(rows, list):
            for r in rows:
                _enrich_row(r)
    return payload


# --- crypto spot reference (#254 / crypto probe ask #216) -----------------
# Crypto barrier markets resolve off a spot index; an agent can't judge ANY
# barrier price without current spot, and the trader probe was forced to leave
# pytheum entirely (Crypto.com MCP) to learn BTC=$63,953. Attach spot_ref on
# crypto rows from a free public source, CACHED (~1 fetch/coin/min) and
# null-fallback so it NEVER blocks or breaks a row. v1 ships spot only — no
# pct_to_strike (fuzzy strike-parse across $150k / ranges / "all time high"
# would ship misleading numbers unsupervised; the agent has the strike + spot).
_COIN_RES: tuple[tuple[str, Any], ...] = (
    ("BTC", re.compile(r"\b(bitcoin|btc)\b", re.I)),
    ("ETH", re.compile(r"\b(ethereum|ether|eth)\b", re.I)),
    ("SOL", re.compile(r"\b(solana|sol)\b", re.I)),
    ("DOGE", re.compile(r"\b(dogecoin|doge)\b", re.I)),
    ("XRP", re.compile(r"\b(xrp|ripple)\b", re.I)),
)
_SPOT_TTL_S = 60.0
_SPOT_CACHE: dict[str, tuple[float | None, float]] = {}  # sym -> (price|None, monotonic)


def _underlying_coin(question: Any) -> str | None:
    """Map a crypto market's question to its underlying ticker (first match wins,
    word-boundary so 'sol'/'eth' don't match inside other words). None if no coin."""
    if not isinstance(question, str):
        return None
    for sym, rgx in _COIN_RES:
        if rgx.search(question):
            return sym
    return None


async def _fetch_spot(sym: str, client: httpx.AsyncClient) -> float | None:
    """Current USD spot from Coinbase's keyless public endpoint. None on any error."""
    try:
        resp = await client.get(f"https://api.coinbase.com/v2/prices/{sym}-USD/spot")
        resp.raise_for_status()
        return float(resp.json()["data"]["amount"])
    except Exception:
        return None


async def _enrich_crypto_spot(rows: Any) -> None:
    """In-place: add spot_ref {symbol, price, source, as_of_age_s} to crypto rows.
    Only touches rows already tagged market_archetype=='crypto'. Cached per coin
    (TTL 60s) and concurrent; degrades to no-op on any failure (never raises)."""
    import asyncio
    if not isinstance(rows, list):
        return
    for r in rows:
        if isinstance(r, dict) and r.get("market_archetype") == "crypto":
            r["_coin"] = _underlying_coin(r.get("question"))
    coins = {r["_coin"] for r in rows
             if isinstance(r, dict) and r.get("_coin")}
    if not coins:
        for r in rows:
            if isinstance(r, dict):
                r.pop("_coin", None)
        return
    now = time.monotonic()
    stale = [s for s in coins
             if s not in _SPOT_CACHE or now - _SPOT_CACHE[s][1] > _SPOT_TTL_S]
    if stale:
        try:
            async with httpx.AsyncClient(timeout=2.5) as client:
                prices = await asyncio.gather(*[_fetch_spot(s, client) for s in stale])
            for s, p in zip(stale, prices, strict=False):
                _SPOT_CACHE[s] = (p, now)
        except Exception:
            pass
    for r in rows:
        if not isinstance(r, dict):
            continue
        sym = r.pop("_coin", None)
        cached = _SPOT_CACHE.get(sym) if sym else None
        if cached and cached[0] is not None:
            r["spot_ref"] = {"symbol": sym, "price": cached[0], "source": "coinbase",
                             "as_of_age_s": round(now - cached[1], 1)}


async def service_status(
    *,
    base_url: str = DEFAULT_BASE,
) -> dict[str, Any]:
    """Fetch the /v1/status endpoint — keyless, no auth required.

    Returns a dict with:
      - ``equivalence``: pairs_loaded + dataset_version
      - ``related``:     pairs_loaded
      - ``platforms``:   per-venue market count + last_updated + "ok"/"stale"
                         (only present when the server has DAO-backed stats)
      - ``service``:     version + now (ISO-8601)
    """
    return await _get("/v1/status", {}, base_url)


async def market_context(
    market_ref: str,
    *,
    base_url: str = DEFAULT_BASE,
    kinds: list[str] | None = None,
    limit: int = 25,
    min_similarity: float | None = None,
) -> dict[str, Any]:
    """Get news/social/macro events paired with a specific market.

    `market_ref` is a venue-prefixed id (polymarket:0x..., kalshi:...) or a slug or URL.
    Returns the market metadata, a ranked list of context items with frozen
    snapshots (title, body, url, published_at), AND `sibling_markets` — correlated
    markets from the same event graph with their volume (and implied YES odds when
    available), a prediction-market-native signal web search can't reproduce.
    """
    market_ref = _normalize_market_ref(market_ref)
    ref_err = _market_ref_error(market_ref)
    if ref_err:
        return ref_err
    sim_err = _range_error(min_similarity, "min_similarity", 0.0, 1.0)
    if sim_err:
        return sim_err
    path = f"/v1/markets/{quote(market_ref, safe='')}/context"
    params: dict[str, Any] = {"limit": limit}
    if kinds:
        params["kinds"] = ",".join(kinds)
    if min_similarity is not None:
        params["min_similarity"] = min_similarity
    resp = await _get_market(path, params, base_url, ref=market_ref, bundle_hint=True)
    if not isinstance(resp, dict) or resp.get("error"):
        return resp
    # Tag the focal market AND each sibling with is_play_money — the screen/find
    # rows carry it but the deep-read surface (context) was dropping it, so an
    # agent that discovers via find then reads via context lost the real-vs-play
    # distinction exactly when comparing a cross-venue sibling.
    if isinstance(resp, dict):
        _enrich_row(resp.get("market"))
        sibs = resp.get("sibling_markets")
        if isinstance(sibs, list):
            for s in sibs:
                _enrich_row(s)
        focal = resp.get("market")
        await _enrich_crypto_spot(([focal] if isinstance(focal, dict) else [])
                                  + (sibs if isinstance(sibs, list) else []))
    return resp


async def bundle_context(
    bundle_ref: str,
    *,
    base_url: str = DEFAULT_BASE,
    kinds: list[str] | None = None,
    limit: int = 50,
    min_similarity: float | None = None,
) -> dict[str, Any]:
    """Get context paired with any market inside a bundle (e.g. '2028 Presidential Election').

    `bundle_ref` is venue-prefixed (polymarket:2028-presidential-election, kalshi:FED-25).
    Events that pair with multiple child markets are deduplicated; the highest-similarity
    hit wins and `matched_market_id` identifies the winning child.
    """
    bundle_ref = _normalize_market_ref(bundle_ref)
    ref_err = _market_ref_error(bundle_ref, param="bundle_ref")
    if ref_err:
        return ref_err
    path = f"/v1/bundles/{quote(bundle_ref, safe='')}/context"
    params: dict[str, Any] = {"limit": limit}
    if kinds:
        params["kinds"] = ",".join(kinds)
    if min_similarity is not None:
        params["min_similarity"] = min_similarity
    resp = await _get_market(path, params, base_url, ref=bundle_ref, kind="bundle")
    if isinstance(resp, dict) and resp.get("error") == "bundle_not_found":
        resp["hint"] += (" A bundle_ref is an EVENT/group id (e.g. 'polymarket:soccer', "
                         "'kalshi:KXNBA-26'), not a single market — for one market use t_market_context.")
    return resp


async def find_markets(
    query: str,
    *,
    base_url: str = DEFAULT_BASE,
    limit: int = 50,
    group_by: str | None = None,
    venue: list[str] | None = None,
    min_similarity: float | None = None,
    exclude_stale: bool = False,
) -> dict[str, Any]:
    """Find prediction markets matching a free-form text query.

    `query` is an article body, an event description, or a question. Returns
    a ranked list of markets across kalshi/polymarket/manifold with similarity
    scores. Pass `group_by="bundle"` to dedupe to one market per bundle.
    """
    if not isinstance(query, str) or not query.strip():
        return {"error": "invalid_query",
                "hint": "query is required — a free-text string (article body, headline, or question)."}
    lerr = _limit_error(limit)
    if lerr:
        return lerr
    params: dict[str, Any] = {"query": query, "limit": limit}
    if group_by is not None:
        gerr = _enum_error(group_by, "group_by", {"bundle", "none"})
        if gerr:
            return gerr
        params["group_by"] = group_by.lower()  # case-fold ('Bundle' was a silent no-op)
    venue_param, verr = _normalize_venues(venue)
    if verr:
        return verr
    sim_err = _range_error(min_similarity, "min_similarity", 0.0, 1.0)
    if sim_err:
        return sim_err
    if venue_param:
        params["venue"] = venue_param
    if min_similarity is not None:
        params["min_similarity"] = min_similarity
    if exclude_stale:
        params["exclude_stale"] = "true"
    resp = _annotate_play_money(await _get("/v1/markets/relevant-to", params, base_url))
    if isinstance(resp, dict):
        await _enrich_crypto_spot(resp.get("markets"))
    return resp


async def event_related_markets(
    event_id: str,
    *,
    base_url: str = DEFAULT_BASE,
    limit: int = 25,
    group_by: str | None = None,
    min_similarity: float | None = None,
) -> dict[str, Any]:
    """Given an event_id from the pytheum-stream firehose, find the markets it
    relates to. Returns 404 if the event is older than the 24h rolling window.
    """
    if not isinstance(event_id, str) or not event_id.strip():
        return {"error": "invalid_event_id",
                "hint": "event_id is required — a firehose event id like 'evt_news_headline_…' "
                        "(from t_market_context's paired events / the stream)."}
    if event_id.strip().lower().startswith(_VENUE_PREFIXES):
        return {"error": "wrong_id_type", "event_id": event_id,
                "hint": (f"'{event_id}' is a market_ref, not an event_id. To find markets related to "
                         "a MARKET use t_market_context; t_event_related_markets takes a firehose "
                         "event id ('evt_…').")}
    path = f"/v1/events/{quote(event_id, safe='')}/related-markets"
    params: dict[str, Any] = {"limit": limit}
    if group_by:
        params["group_by"] = group_by
    if min_similarity is not None:
        params["min_similarity"] = min_similarity
    resp = await _get_market(path, params, base_url, ref=event_id, kind="event")
    if isinstance(resp, dict) and resp.get("error") == "event_not_found":
        resp["hint"] = (f"No event '{event_id}'. event_ids look like 'evt_news_headline_…' and only "
                        "live in the 24h rolling window (older events expire).")
    return resp


async def market_history(market_ref, *, base_url=DEFAULT_BASE, since=None, until=None,
                         limit=500, full=False):
    """PIT price+book series + moves (move_1h/24h/7d) — pytheum's own capture."""
    market_ref = _normalize_market_ref(market_ref)
    ref_err = _market_ref_error(market_ref)
    if ref_err:
        return ref_err
    if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1:
        return {"error": "invalid_limit", "limit": limit,
                "hint": "limit must be an integer >= 1 (max price points to return; capped at 2000)."}
    path = f"/v1/markets/{quote(market_ref, safe='')}/history"
    params = {"limit": limit, "since": since, "until": until}
    if full:
        params["full"] = "true"
    resp = await _get_market(path, params, base_url, ref=market_ref, bundle_hint=True)
    if not isinstance(resp, dict) or resp.get("error"):
        return resp
    # Empty series is indistinguishable from "no such data" — the probe drilled into
    # a bundle PARENT (polymarket:30615) and got count:0 with no explanation. Self-explain.
    if not resp.get("count") and not resp.get("points"):
        resp["hint"] = (f"No PIT price series for '{market_ref}'. Either it's a bundle/event PARENT "
                        "(query an outcome leg — parents have no own price), or the market is too "
                        "new/illiquid to have captured spaced points yet.")
    return resp


async def market_flow(market_ref, *, base_url=DEFAULT_BASE, window_hours=24):
    """Wallet-level trade flow for a Polymarket market."""
    market_ref = _normalize_market_ref(market_ref)
    ref_err = _market_ref_error(market_ref)
    if ref_err:
        return ref_err
    path = f"/v1/markets/{quote(market_ref, safe='')}/flow"
    return await _get_market(path, {"window_hours": window_hours}, base_url,
                             ref=market_ref, bundle_hint=True)


async def screen_markets(*, base_url=DEFAULT_BASE, venues=None, status="active",
                         min_volume=None, max_volume=None, min_liquidity=None,
                         resolves_before=None, resolves_after=None, sort_by="volume",
                         limit=50, exclude_stale=False):
    """Structured (non-semantic) market screen — one call vs N semantic searches."""
    lerr = _limit_error(limit)
    if lerr:
        return lerr
    venues_param, verr = _normalize_venues(venues)
    if verr:
        return verr
    serr = _enum_error(sort_by, "sort_by", {"volume", "liquidity", "resolution", "move"})
    if serr:
        return serr
    # status is an open set server-side, so don't hard-reject — but case-fold the
    # common 'ACTIVE' mistake (was a silent empty) and map all/any → all statuses.
    if isinstance(status, str):
        status = status.strip().lower() or "active"
        if status in ("any", "all"):
            status = None
    for _p, _v in (("resolves_before", resolves_before), ("resolves_after", resolves_after)):
        derr = _date_error(_v, _p)
        if derr:
            return derr
    # Inverted window → silent empty (probe r2). Flag it instead.
    if (_date_error(resolves_before, "resolves_before") is None
            and _date_error(resolves_after, "resolves_after") is None
            and resolves_before and resolves_after):
        from datetime import datetime as _dt
        if (_dt.fromisoformat(str(resolves_after).replace("Z", "+00:00"))
                > _dt.fromisoformat(str(resolves_before).replace("Z", "+00:00"))):
            return {"error": "empty_window",
                    "hint": f"resolves_after ({resolves_after}) is later than resolves_before "
                            f"({resolves_before}) — the window is empty. Swap them?"}
    # Inverted volume window → silent empty, same class as the date guard (probe r7).
    if (isinstance(min_volume, (int, float)) and not isinstance(min_volume, bool)
            and isinstance(max_volume, (int, float)) and not isinstance(max_volume, bool)
            and min_volume > max_volume):
        return {"error": "empty_window",
                "hint": f"min_volume ({min_volume}) is greater than max_volume ({max_volume}) — "
                        "the volume window is empty. Swap them?"}
    params = {"venues": venues_param, "status": status,
              "min_volume": min_volume, "max_volume": max_volume,
              "min_liquidity": min_liquidity,
              "resolves_before": resolves_before, "resolves_after": resolves_after,
              "sort_by": sort_by, "limit": limit}
    if exclude_stale:
        params["exclude_stale"] = "true"
    resp = _annotate_play_money(await _get("/v1/markets/screen", params, base_url))
    if isinstance(resp, dict):
        await _enrich_crypto_spot(resp.get("markets"))
        # Re-order the returned page onto ONE cross-venue axis when sorting by
        # volume. Raw volume_usd is contracts on Kalshi vs USD on Polymarket, so
        # the server's raw-volume order over-ranks Kalshi (#257); volume_usd_norm
        # is comparable. This only re-orders the page (candidate set is still
        # selected server-side by raw venue volume); None (Manifold) sorts last.
        if sort_by == "volume" and isinstance(resp.get("markets"), list):
            resp["markets"].sort(
                key=lambda r: (r.get("volume_usd_norm") is not None,
                               r.get("volume_usd_norm") or 0.0),
                reverse=True)
            if isinstance(resp.get("meta"), dict):
                resp["meta"]["sorted_by"] = "volume_usd_norm"
        # status is free-form server-side (Kalshi passes raw statuses through), so we
        # can't hard-reject — but a typo'd status ('garbage'/'open') silently yields
        # [] like a real no-match. If empty + an uncommon status, say so (probe r4 P1).
        if (not resp.get("markets") and isinstance(status, str)
                and status not in _COMMON_STATUSES):
            resp["hint"] = (f"0 markets for status={status!r}. status is free-form; common values "
                            "are active, resolved, closed (case-insensitive; omit or 'all'/'any' "
                            "for every status). If you expected results, check the status value.")
    return resp


# Essential trader fields kept on each market in BATCH mode. The full
# t_market_context market object (~3KB: resolution text, condition_id,
# resolution_window_years, is_play_money, …) x 25 refs blows the response token
# ceiling and spills to a file, which hides the batch's own ok_count/error_count
# (the original P0 — and a trader probe re-hit it with 25 REAL markets even after
# the count-collapse digest, because the per-ref market block itself was still
# fat). Breadth lives in the batch; an agent drills into ONE ref with
# t_market_context for the full object + resolution + condition_id + legs.
_BATCH_MARKET_KEYS = (
    "id", "question", "venue", "bundle_id", "status", "implied_yes", "book",
    "volume_usd_norm", "liquidity_usd", "taker_fee_bps", "flow_flag",
    "days_to_resolution", "is_stale", "resolution_status", "market_archetype",
    "spot_ref",
)


def _compact_batch_item(r: Any, *, body_chars: int = 140, max_ctx: int = 3) -> Any:
    """Reduce a per-ref context result to a lean DIGEST for batch mode: a market
    CORE (essential trader fields only — _BATCH_MARKET_KEYS), top-N context
    headlines (no heavy snapshot/body), and counts for the heavy structural lists
    (sibling_markets / bundle_children / bundle_outcomes). Drops the verbose
    per-ref meta block and the full market object so 25 real markets fit inline
    under the token ceiling. Single-call t_market_context is unchanged."""
    if not isinstance(r, dict) or r.get("error"):
        return r
    out: dict[str, Any] = {}
    # market core: essentials only (drill into one ref for the full object)
    m = r.get("market")
    if isinstance(m, dict):
        out["market"] = {k: m[k] for k in _BATCH_MARKET_KEYS if m.get(k) is not None}
    # heavy structural lists -> counts (top-level and nested under market)
    for h in (r, m):
        if isinstance(h, dict):
            for lk in ("sibling_markets", "bundle_children", "bundle_outcomes"):
                lst = h.get(lk)
                if isinstance(lst, list):
                    out[f"{lk}_count"] = len(lst)
    # context: top-N headlines, short excerpt, no heavy snapshot text
    ctx = r.get("context") or []
    lean_ctx = []
    for it in ctx[:max_ctx]:
        if not isinstance(it, dict):
            continue
        ex = it.get("excerpt") or it.get("body") or ""
        if isinstance(ex, str) and len(ex) > body_chars:
            ex = ex[:body_chars] + "…"
        lean_ctx.append({k: it.get(k) for k in ("kind", "title", "url", "published_at",
                                                "similarity") if it.get(k) is not None}
                        | ({"excerpt": ex} if ex else {}))
    if lean_ctx:
        out["context"] = lean_ctx
    if len(ctx) > len(lean_ctx):
        out["context_total"] = len(ctx)  # disclose the truncation
    return out


async def context_batch(market_refs, *, base_url=DEFAULT_BASE, limit=8):
    """Batch /context over up to 25 refs in one call; one bad ref never sinks the batch."""
    import asyncio
    if isinstance(market_refs, str) or not isinstance(market_refs, list) or not market_refs:
        return {"error": "invalid_market_refs",
                "hint": ("market_refs must be a non-empty LIST of venue-prefixed ids "
                         f"({_REF_EXAMPLES}) — not a single string. "
                         "e.g. [\"kalshi:KXNBA-26-NYK\", \"polymarket:558936\"].")}
    submitted = len(market_refs)
    # dedup (preserve order) after normalizing, then cap at 25 — disclose all three.
    seen: set = set()
    deduped: list = []
    for r in market_refs:
        nr = _normalize_market_ref(r)
        key = nr if isinstance(nr, str) else repr(nr)
        if key not in seen:
            seen.add(key)
            deduped.append(nr)
    refs = deduped[:25]
    # Bound fan-out: 25 concurrent calls to our OWN HTTP shim trip the single-loop
    # concurrency cliff (#244) and ~all but a couple drop with RemoteProtocolError,
    # while the batch still reports "success" (probe r3 P0). Cap concurrency + one
    # retry so a full screen-page enrichment doesn't silently lose most rows.
    sem = asyncio.Semaphore(5)
    async def _one(ref):
        async with sem:
            for attempt in range(2):
                try:
                    return ref, _compact_batch_item(
                        await market_context(ref, base_url=base_url, limit=limit))
                except Exception:
                    if attempt == 0:
                        await asyncio.sleep(0.25)
            return ref, {"error": "upstream_unavailable", "ref": ref,
                         "hint": "context fetch failed (transient/load) after a retry — "
                                 "request this ref alone via t_market_context."}
    results = dict(await asyncio.gather(*[_one(r) for r in refs]))
    n_err = sum(1 for v in results.values() if isinstance(v, dict) and v.get("error"))
    out = {"results": results, "count": len(results),
           "ok_count": len(results) - n_err, "error_count": n_err}
    if submitted != len(refs):
        dropped = submitted - len(refs)
        # Top-level `note` (not just meta) — a probe agent missed the buried meta
        # disclosure and reported the truncation as silent. Mirror the
        # t_find_divergences note pattern that agents actually read.
        out["note"] = (f"{submitted} refs submitted, {len(refs)} processed ({dropped} dropped: "
                       f"duplicates and/or over the {25}-ref cap). Split into ≤25-ref batches "
                       "to cover them all.")
        out["meta"] = {"submitted": submitted, "deduped_to": len(deduped),
                       "processed": len(refs), "cap": 25}
    return out


async def matched_pairs(
    *,
    bet_type: str | None = None,
    query: str | None = None,
    min_volume: float | None = None,
    sort_by: str = "volume",
    limit: int = 25,
    offset: int = 0,
    league: str | None = None,
    date: str | None = None,
    fungible_only: bool = False,
    base_url: str = DEFAULT_BASE,
) -> dict[str, Any]:
    """Browse the pytheum cross-venue matched pairs dataset.

    Accepts the same query parameters as GET /v1/markets/matched.
    ``sort_by`` controls ordering: 'volume' (default), 'spread' (arb radar —
    biggest cross-venue price disagreements first), or 'confidence'.
    ``league`` filters to a specific league (e.g. 'NBA', 'NFL'); rows without
    a ``league`` field are excluded when this filter is active.
    ``date`` (YYYY-MM-DD) filters to a specific game date; rows without a
    ``game_date`` field are excluded when this filter is active.
    ``fungible_only`` restricts to pairs whose method is deterministic /
    structural / human-adjudicated (no LLM-judged pairs).
    """
    if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1:
        return {"error": "invalid_limit", "limit": limit,
                "hint": "limit must be an integer >= 1 (max 200)."}
    params: dict[str, Any] = {"limit": min(limit, 200), "offset": max(0, offset)}
    if bet_type:
        params["bet_type"] = bet_type
    if query:
        params["q"] = query
    if min_volume is not None:
        params["min_volume"] = min_volume
    if sort_by and sort_by != "volume":
        params["sort_by"] = sort_by
    if league:
        params["league"] = league
    if date:
        params["date"] = date
    if fungible_only:
        params["fungible_only"] = "true"
    return await _get("/v1/markets/matched", params, base_url)


async def market_rules(
    market_ref: str,
    *,
    base_url: str = DEFAULT_BASE,
) -> dict[str, Any]:
    """Get resolution rules text for a market and its cross-venue equivalent.

    Returns the full settlement rules text for the focal market AND the
    verified counterpart from the equivalence dataset side by side, with
    deadline comparison.  `market_ref` must be venue-prefixed — 'kalshi:...'
    or 'polymarket:...'.
    """
    market_ref = _normalize_market_ref(market_ref)
    ref_err = _market_ref_error(market_ref)
    if ref_err:
        return ref_err
    path = f"/v1/markets/{quote(market_ref, safe='')}/rules"
    return await _get_market(path, {}, base_url, ref=market_ref)


async def related_markets(
    market_ref: str,
    *,
    base_url: str = DEFAULT_BASE,
) -> dict[str, Any]:
    """Correlated cross-venue markets that are NOT settlement-equivalent.

    Hedge discovery on verified correlations from pytheum's matcher: each row
    carries the relation type, both venues' bands, and a basis note explaining
    exactly how settlement differs.
    """
    market_ref = _normalize_market_ref(market_ref)
    ref_err = _market_ref_error(market_ref)
    if ref_err:
        return ref_err
    path = f"/v1/markets/{quote(market_ref, safe='')}/related"
    return await _get_market(path, {}, base_url, ref=market_ref)


async def equivalent_markets(
    market_ref: str,
    *,
    base_url: str = DEFAULT_BASE,
) -> dict[str, Any]:
    """Find the same market on the other venue from pytheum's equivalence dataset.

    Returns the focal market metadata, a list of verified counterpart(s) with live
    prices (nulled when the counterpart isn't in the platform store), and the
    cross-venue spread. `market_ref` must be venue-prefixed — 'kalshi:...' or
    'polymarket:...'.
    """
    market_ref = _normalize_market_ref(market_ref)
    ref_err = _market_ref_error(market_ref)
    if ref_err:
        return ref_err
    path = f"/v1/markets/{quote(market_ref, safe='')}/equivalents"
    resp = await _get_market(path, {}, base_url, ref=market_ref)
    return resp


async def orderbook(
    market_ref: str,
    depth: int = 20,
    *,
    base_url: str = DEFAULT_BASE,
) -> dict[str, Any]:
    """Fetch the live orderbook for a market.

    Calls the venue API in real time (not cached here; the server coalesces
    concurrent requests for the same key and caches results for ~2 s). Pass
    ``depth`` to limit the number of price levels returned (1–200, default 20).
    ``market_ref`` must be venue-prefixed — 'kalshi:KXNBA-26-NYK' or
    'polymarket:some-slug'. Returns bids/asks as [[price, size], ...] plus a
    top-of-book summary (bid/ask/spread/mid) and source:"live".
    On any venue error, returns 200 with source:"unavailable".
    """
    ref_err = _market_ref_error(market_ref)
    if ref_err:
        return ref_err
    path = f"/v1/markets/{quote(market_ref.strip(), safe='')}/book"
    return await _get(path, {"depth": depth}, base_url)


async def recent_trades(
    market_ref: str,
    limit: int = 50,
    *,
    base_url: str = DEFAULT_BASE,
) -> dict[str, Any]:
    """Fetch recent trades for a market (live venue fetch).

    Coalesced + cached ~10 s server-side. ``market_ref`` must be venue-prefixed.
    Returns ``{trades: [{ts, price, size, side}, ...], count, venue, source:"live"}``.
    On any venue error, returns source:"unavailable".
    """
    ref_err = _market_ref_error(market_ref)
    if ref_err:
        return ref_err
    path = f"/v1/markets/{quote(market_ref.strip(), safe='')}/trades"
    return await _get(path, {"limit": limit}, base_url)


async def open_interest(
    market_ref: str,
    *,
    base_url: str = DEFAULT_BASE,
) -> dict[str, Any]:
    """Fetch the current open interest for a market (live venue fetch).

    Coalesced + cached ~30 s server-side. ``market_ref`` must be venue-prefixed.
    Returns ``{open_interest: float|null, venue, ref, source:"live"}``.
    On any venue error, returns source:"unavailable".
    """
    ref_err = _market_ref_error(market_ref)
    if ref_err:
        return ref_err
    path = f"/v1/markets/{quote(market_ref.strip(), safe='')}/oi"
    return await _get(path, {}, base_url)


async def leaderboard(
    period: str = "weekly",
    *,
    base_url: str = DEFAULT_BASE,
) -> dict[str, Any]:
    """Fetch the Polymarket trader leaderboard.

    Polymarket-only — Kalshi trades are anonymized.
    Coalesced + cached 300 s server-side.
    ``period`` is 'weekly' or 'monthly'.
    Returns {period, traders: [{name, address, profit, volume, rank}], count, source, venue}.
    """
    return await _get("/v1/traders/leaderboard", {"period": period}, base_url)


async def trader_profile(
    wallet: str,
    *,
    base_url: str = DEFAULT_BASE,
) -> dict[str, Any]:
    """Fetch a Polymarket trader's positions, recent activity, and portfolio value.

    Polymarket-only — Kalshi trades are anonymized.
    Coalesced + cached 60 s server-side.
    ``wallet`` is a 0x-hex address or Polymarket username.
    Returns {wallet, positions[], activity[], value, meta}.
    """
    return await _get(f"/v1/traders/{quote(wallet.strip(), safe='')}", {}, base_url)


async def market_holders(
    market_ref: str,
    *,
    base_url: str = DEFAULT_BASE,
) -> dict[str, Any]:
    """Fetch token holders for a Polymarket market.

    Polymarket-only — Kalshi trades are anonymized (no holder breakdown available).
    ``market_ref`` must be venue-prefixed 'polymarket:…'.
    Coalesced + cached 60 s server-side.
    Returns {holders: [{address, amount, outcome}], count, ref, source, venue}.
    On any venue error returns source:"unavailable".
    """
    ref_err = _market_ref_error(market_ref)
    if ref_err:
        return ref_err
    path = f"/v1/markets/{quote(market_ref.strip(), safe='')}/holders"
    return await _get(path, {}, base_url)


async def whale_trades(
    min_usd: float = 500,
    limit: int = 50,
    market_ref: str | None = None,
    *,
    base_url: str = DEFAULT_BASE,
) -> dict[str, Any]:
    """Recent large-notional Polymarket trades (notional_usd = size * price >= min_usd).

    Polymarket-only — Kalshi trades are anonymized.
    Live venue fetch, coalesced + cached 30 s server-side.
    ``market_ref`` (optional, venue-prefixed 'polymarket:…') filters to one market.
    ``min_usd``: minimum notional USD threshold. Default 500.
    ``limit``: max results returned (1–500). Default 50.
    Returns {trades: [{ts, market, price, size, notional_usd, side, wallet, pseudonym?}],
    count, min_usd, venue, source}.
    On any venue error returns source:"unavailable".
    """
    params: dict[str, Any] = {"min_usd": min_usd, "limit": limit}
    if market_ref is not None:
        ref_err = _market_ref_error(market_ref)
        if ref_err:
            return ref_err
        params["market_ref"] = market_ref.strip()
    return await _get("/v1/markets/whale-trades", params, base_url)


async def ohlcv(
    market_ref: str,
    *,
    interval: str = "1h",
    since: str | None = None,
    until: str | None = None,
    limit: int = 200,
    base_url: str = DEFAULT_BASE,
) -> dict[str, Any]:
    """Fetch OHLCV candles for a market.

    Thin wrapper around GET /v1/markets/{ref}/ohlcv.  ``market_ref`` must be
    venue-prefixed.  Returns
    ``{market, interval, candles:[{t,o,h,l,c,v}], meta:{source,count,partial_last_bucket}}``.
    """
    ref_err = _market_ref_error(market_ref)
    if ref_err:
        return ref_err
    ref = _normalize_market_ref(market_ref)
    return await _get_market(
        f"/v1/markets/{quote(ref, safe='')}/ohlcv",
        {"interval": interval, "since": since, "until": until, "limit": limit},
        base_url,
        ref=ref,
    )


_TITLE_STOP = {"will", "the", "a", "an", "by", "in", "of", "to", "is", "are", "be",
               "win", "wins", "before", "after", "than", "2024", "2025", "2026",
               "2027", "2028", "2029"}


def _title_sim(a: str, b: str) -> float:
    """Jaccard over content tokens — a same-question guard so the divergence
    scanner doesn't pair (and rank the gap of) two different questions."""
    def toks(s: str) -> set[str]:
        return {w for w in re.findall(r"[a-z0-9]+", (s or "").lower())
                if len(w) > 2 and w not in _TITLE_STOP}
    ta, tb = toks(a), toks(b)
    return len(ta & tb) / len(ta | tb) if (ta and tb) else 0.0


def _divergence_edge(a_book: Any, b_book: Any) -> float | None:
    """Net-of-fees LOCKED cross-venue edge between two books of the SAME question:
    buy YES on the cheaper venue + buy NO on the dearer one. Uses yes_ask_net /
    no_ask_net (#250) so the number is already fee-adjusted. Returns 1 - min(cost)
    (positive = a real locked arb) or None if the net fields aren't both present."""
    if not isinstance(a_book, dict) or not isinstance(b_book, dict):
        return None
    costs = []
    for ya, nb in ((a_book.get("yes_ask_net"), b_book.get("no_ask_net")),
                   (b_book.get("yes_ask_net"), a_book.get("no_ask_net"))):
        if isinstance(ya, (int, float)) and isinstance(nb, (int, float)):
            costs.append(ya + nb)
    return round(1 - min(costs), 4) if costs else None


def _lock_days(a: Any, b: Any) -> float | None:
    """Capital in a cross-venue lock is tied until BOTH legs settle, so the
    binding horizon is the LATER of the two resolutions. Returns max of the two
    legs' days_to_resolution (positive only), or whichever is known, else None."""
    ds = [d for d in (a, b) if isinstance(d, (int, float)) and d > 0]
    return max(ds) if ds else None


def _annualized_edge(net_edge: Any, days: Any) -> float | None:
    """Annualize a locked net edge over its capital-lockup horizon so an agent
    ranks by capital efficiency, not raw gap (trader ask #4: a 2.26% lock over
    887d should sort BELOW a 1% lock resolving next week). Naive compounding:
    (1+edge)^(365/days) - 1. None when edge/days missing or non-positive days."""
    if not isinstance(net_edge, (int, float)) or not isinstance(days, (int, float)) or days <= 0:
        return None
    try:
        # Cap at 1000 (100,000% APY): sub-day locks compound to absurdity
        # (a 0.46 edge over 0.92d annualizes to 1e65) and would dominate any
        # sort — beyond the cap the number carries no information anyway.
        return min(round((1 + net_edge) ** (365.0 / max(days, 0.5)) - 1, 4), 1000.0)
    except (OverflowError, ValueError):
        return None


def _orient_poly_leg(leg: dict[str, Any], side: Any) -> dict[str, Any]:
    """Re-orient a Polymarket game-market leg so its quote refers to the SAME
    side as the Kalshi leg. Poly game markets quote their FIRST-LISTED outcome;
    when the mapped side is index 1 the quote must be complemented (implied' =
    1-implied; bid' = 1-ask, ask' = 1-bid; sizes swap)."""
    if side != 1:
        return leg
    out = dict(leg)
    iy = leg.get("implied_yes")
    if isinstance(iy, (int, float)):
        out["implied_yes"] = round(1 - iy, 4)
    book = leg.get("book")
    if isinstance(book, dict):
        nb: dict[str, Any] = {}
        if isinstance(book.get("ask"), (int, float)):
            nb["bid"] = round(1 - book["ask"], 4)
        if isinstance(book.get("bid"), (int, float)):
            nb["ask"] = round(1 - book["bid"], 4)
        if isinstance(book.get("last"), (int, float)):
            nb["last"] = round(1 - book["last"], 4)
        if isinstance(book.get("spread"), (int, float)):
            nb["spread"] = book["spread"]
        if isinstance(book.get("day_change"), (int, float)):
            nb["day_change"] = round(-book["day_change"], 4)
        if book.get("ask_size") is not None:
            nb["bid_size"] = book["ask_size"]
        if book.get("bid_size") is not None:
            nb["ask_size"] = book["bid_size"]
        out["book"] = nb or None
    return out


def _div_leg(leg: dict[str, Any]) -> dict[str, Any]:
    """Output shape for one divergence leg (same fields the v1 scanner emitted)."""
    return {"market_id": leg.get("id"), "venue": leg.get("venue"),
            "question": leg.get("question"), "implied_yes": leg.get("implied_yes"),
            "book": leg.get("book"), "days_to_resolution": leg.get("days_to_resolution"),
            "last_move_age_s": leg.get("last_move_age_s"),
            "is_parked_wall": leg.get("is_parked_wall")}


_RULES_TRUNC = 400  # chars per venue resolution text in divergence rows


def _trunc_rules(text: str | None) -> str | None:
    """Truncate resolution text to _RULES_TRUNC chars for the divergences payload."""
    if not text:
        return None
    return text[:_RULES_TRUNC] + ("…" if len(text) > _RULES_TRUNC else "")


async def find_divergences(*, base_url: str = DEFAULT_BASE, min_net_edge: float = 0.0,
                           limit: int = 10, seed_limit: int = 12,
                           include_rules: bool = True,
                           fungible_only: bool = False) -> dict[str, Any]:
    """v2 cross-venue net-of-fees divergence scanner (#251/#247). Pairs come
    from the VERIFIED equivalence set — the matcher's pre-decided gold pairs
    (132,946 shipped 2026-06-11; we serve them, we don't match) joined to live
    books via /v1/markets/equivalents. Each pair carries `matched_by` (the
    method that decided it: spread_match / game_title_match / human_adjudicated
    / ...) and `match_confidence` (judged slice only) so a wrong match is
    diagnosable, never silent. Replaces the v1 8-seed fuzzy-sibling scan that
    missed differently-phrased twins (the live NBA Finals pair). `seed_limit`
    is retained for call compatibility but unused.

    ``include_rules`` (default True): each divergence row gains
    ``resolution: {kalshi, polymarket}`` with settlement rules text truncated
    to 400 chars — pull once here instead of N t_market_rules calls.
    ``fungible_only`` (default False): restrict to deterministic/structural
    pairs (no LLM-judged pairs).
    """
    _ = seed_limit  # v1 compat — pair recall now comes from the equivalence set
    if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1:
        return {"error": "invalid_limit", "limit": limit,
                "hint": "limit must be an integer >= 1 (it's the max pairs to return). "
                        "limit=0 would yield an empty list that looks like 'no divergences found'."}
    _eq_params: dict[str, Any] = {"limit": 150}
    if include_rules:
        _eq_params["include_rules"] = "true"
    if fungible_only:
        _eq_params["fungible_only"] = "true"
    resp = await _get("/v1/markets/equivalents", _eq_params, base_url)
    pairs = resp.get("pairs") or []
    if not pairs and "error" in resp:
        return {"error": "equivalents_unavailable", "detail": resp.get("error"),
                "hint": "the verified-pair endpoint failed; retry, or fall back to "
                        "t_screen + t_market_context sibling_markets for a manual "
                        "cross-venue read."}
    out: list[dict[str, Any]] = []
    orientation_excluded = 0
    parked_excluded = 0
    suspect_excluded = 0
    for p in pairs:
        a, b = p.get("a") or {}, p.get("b") or {}
        # ORIENTATION GATE: game/tennis/esports moneyline pairs map MARKET to
        # MARKET (correct equivalence), but the Polymarket game market's
        # implied_yes is its FIRST-LISTED outcome — which may be the OTHER team
        # than the Kalshi leg's ticker side (verified live: Kalshi 'Will Moutet
        # win' 0.08 vs poly 'Kyrgios vs Moutet' 0.9995 = Kyrgios side). Pairs
        # with a verified side mapping (pair_side_map, scripts/map_pair_sides)
        # are re-oriented and scored; unmapped non-event pairs are excluded,
        # never guessed.
        side = p.get("poly_side")
        if p.get("bet_type") not in ("event",) and side is None:
            orientation_excluded += 1
            continue
        if side is not None:
            b = _orient_poly_leg(b, side)
        # A parked-wall leg is a frozen resting order, not a live price —
        # settled-but-unresolved games (Kalshi settles T+1) otherwise dominate
        # the ranking with phantom locked edges.
        if a.get("is_parked_wall") or b.get("is_parked_wall"):
            parked_excluded += 1
            continue
        for leg in (a, b):
            _net_book(leg.get("venue"), leg.get("book"))
        edge = _divergence_edge(a.get("book"), b.get("book"))
        if edge is None or edge < min_net_edge:
            continue
        # PLAUSIBILITY GUARD on side-mapped pairs: a >15pt locked arb on live
        # liquid game markets does not persist in reality — such an "edge" is a
        # venue time-skew (one quote mid-game, the other lagging) or a residual
        # side issue, so exclude rather than rank phantom money first.
        if p.get("bet_type") not in ("event",) and edge > 0.15:
            suspect_excluded += 1
            continue
        lock_days = _lock_days(a.get("days_to_resolution"), b.get("days_to_resolution"))
        row: dict[str, Any] = {
            "net_edge": edge,
            # capital efficiency: annualize over the binding (later) leg so a
            # tiny near-term lock outranks a bigger one tied up for years (#4).
            "annualized_net_edge": _annualized_edge(edge, lock_days),
            "lock_days": lock_days,
            "matched_by": p.get("method"),
            "match_confidence": p.get("confidence"),
            "bet_type": p.get("bet_type"),
            # For side-mapped game markets: which poly outcome the quotes refer
            # to after re-orientation (both legs now quote the SAME side).
            "poly_outcome": p.get("poly_outcome"),
            "title_similarity": round(
                _title_sim(a.get("question") or "", b.get("question") or ""), 2),
            # either_leg_parked: a parked-wall quote (frozen behind a tight
            # spread) is NOT a live tradeable price, so the net_edge off it is
            # a ghost — the recurring false edge the probes flagged (Bernie
            # 2028 longshot pair). last_move_age_s/is_parked_wall ride each leg
            # so the agent can filter without a per-leg t_market_history call.
            "either_leg_parked": bool(a.get("is_parked_wall") or b.get("is_parked_wall")),
            "a": _div_leg(a),
            "b": _div_leg(b),
        }
        if include_rules:
            # Bundle settlement rules text for both venues so the agent can
            # verify resolution semantics without a separate t_market_rules call.
            # Legs carry `resolution` when include_rules was forwarded to the
            # equivalents endpoint.  Truncated to _RULES_TRUNC chars here
            # (equivalents leg already uses _MAX_RESOLUTION_CHARS=8000; we
            # apply the tighter divergences limit at this aggregation point).
            # Kalshi is leg `a`, Polymarket is leg `b` by the equivalents
            # collection convention (a=kalshi_market_id, b=polymarket_market_id).
            row["resolution"] = {
                "kalshi": _trunc_rules(a.get("resolution")),
                "polymarket": _trunc_rules(b.get("resolution")),
            }
        out.append(row)
    # Rank by annualized edge (capital efficiency) when known, else raw net_edge;
    # a pair with no horizon falls back to its raw edge so it isn't lost.
    out.sort(key=lambda d: (d.get("annualized_net_edge") if d.get("annualized_net_edge")
                            is not None else d["net_edge"]), reverse=True)
    return {
        "divergences": out[:limit],
        "pairs_scanned": len(pairs),
        "orientation_excluded": orientation_excluded,
        "parked_excluded": parked_excluded,
        "suspect_excluded": suspect_excluded,
        "ranked_by": "annualized_net_edge (capital-efficiency; falls back to net_edge when horizon unknown)",
        "note": ("Pairs are pre-decided by the cross-venue matcher (gold set) and "
                 "joined to live books server-side; `matched_by` is the per-pair "
                 "provenance (deterministic structural methods carry null "
                 "match_confidence by design). net_edge is fee-adjusted "
                 "(yes_ask_net/no_ask_net). Edge-scored: binary event pairs + "
                 "side-mapped winner-style moneylines (`poly_outcome` names the "
                 "side both quotes refer to after re-orientation). Excluded with "
                 "counts: unmapped orientations, frozen parked-wall pairs, and "
                 "suspect >15pt 'edges' on game pairs (venue time-skew, not free "
                 "money) — all still served raw via /v1/markets/equivalents. On "
                 "fast-moving live games, compare each leg's last_move_age_s "
                 "before trusting a gap. Manifold excluded (play money)."),
    }
