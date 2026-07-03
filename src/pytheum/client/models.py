"""Typed response models for the pytheum Python SDK.

Lightweight dataclasses only — no pydantic, no extra deps beyond stdlib. Every
model exposes a **lenient** ``from_dict`` classmethod: unknown keys are
ignored (new API fields never break the client), missing keys default to
``None``/empty, and the full original payload is preserved on ``.raw`` as an
escape hatch.

Shapes are grounded in live samples pulled from ``https://api.pytheum.com``
on 2026-07-03 (see the design spec, ``docs/specs/2026-07-03-python-sdk-design.md``)
for every endpoint except a couple of thin/rare ones noted inline.

## What's modeled vs. left as passthrough

The registry (``_registry.py``) has 25 endpoints. The ones with a rich,
stable shape get typed dataclasses below. A handful are deliberately
**thin/variable** — ``about``, ``guide``, ``quality``, ``context``,
``bundle_context``, ``context_batch``, ``event_related_markets``,
``flow``, ``history``, and ``rules`` — and are left as plain ``dict``
(the API's raw JSON) per the design spec ("a permissive passthrough model
or dict return is fine"). The client method layer can return these
untouched; there is nothing to wrap here.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from dataclasses import fields as _dc_fields
from datetime import datetime
from typing import Any, Self, TypeVar

__all__ = [
    "Status",
    "PlatformStat",
    "MarketLeg",
    "CrossVenue",
    "MatchedPair",
    "MatchedPage",
    "Divergence",
    "BookTop",
    "Market",
    "SearchPage",
    "ScreenPage",
    "Equivalent",
    "EquivalentsResult",
    "RelatedMarket",
    "RelatedResult",
    "BookLevel",
    "Orderbook",
    "Trade",
    "TradesPage",
    "OHLCVBar",
    "OHLCVSeries",
    "TraderPosition",
    "Trader",
    "LeaderboardEntry",
    "LeaderboardResult",
    "Holder",
    "HoldersPage",
    "WhaleTrade",
    "WhaleTradesPage",
]


# --------------------------------------------------------------------------
# shared helpers
# --------------------------------------------------------------------------

_T = TypeVar("_T")


def _parse_ts(value: Any) -> datetime | None:
    """Parse an ISO-8601 timestamp. Returns None on any failure — never raises."""
    if not isinstance(value, str) or not value:
        return None
    v = value.strip()
    if v.endswith("Z"):
        v = v[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(v)
    except ValueError:
        return None


def _autofill(cls: type[_T], d: dict[str, Any] | None, **overrides: Any) -> _T:
    """Build ``cls`` from ``d`` by matching dataclass field names 1:1 to dict
    keys, then applying ``overrides`` for fields that need custom parsing or
    nested construction. Missing keys become None; unknown keys are dropped
    (they're still visible on ``.raw``). Shared by every model whose fields
    line up directly with the API's key names.
    """
    d = d if isinstance(d, dict) else {}
    kwargs: dict[str, Any] = {}
    for f in _dc_fields(cls):  # type: ignore[arg-type]  # _T is always a dataclass here
        if f.name == "raw" or f.name in overrides:
            continue
        kwargs[f.name] = d.get(f.name)
    kwargs.update(overrides)
    kwargs["raw"] = dict(d)
    return cls(**kwargs)


# --------------------------------------------------------------------------
# /v1/status
# --------------------------------------------------------------------------

@dataclass(slots=True)
class PlatformStat:
    """One entry of ``/v1/status``'s ``platforms`` map (e.g. "kalshi")."""

    markets: int | None = None
    last_updated: datetime | None = None
    status: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Self:
        return _autofill(cls, d, last_updated=_parse_ts((d or {}).get("last_updated")))


@dataclass(slots=True)
class Status:
    """``GET /v1/status`` — service health + dataset freshness."""

    equivalence_pairs_loaded: int | None = None
    equivalence_dataset_version: str | None = None
    related_pairs_loaded: int | None = None
    hl_related_pairs_loaded: int | None = None
    hl_dataset_version: str | None = None
    service_version: str | None = None
    now: datetime | None = None
    platforms: dict[str, PlatformStat] = field(default_factory=dict)
    raw: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Self:
        d = d if isinstance(d, dict) else {}
        equivalence = d.get("equivalence") or {}
        related = d.get("related") or {}
        hl_related = d.get("hl_related") or {}
        service = d.get("service") or {}
        platforms_raw = d.get("platforms") or {}
        platforms = {
            name: PlatformStat.from_dict(p)
            for name, p in platforms_raw.items()
            if isinstance(p, dict)
        }
        return cls(
            equivalence_pairs_loaded=equivalence.get("pairs_loaded"),
            equivalence_dataset_version=equivalence.get("dataset_version"),
            related_pairs_loaded=related.get("pairs_loaded"),
            hl_related_pairs_loaded=hl_related.get("pairs_loaded"),
            hl_dataset_version=hl_related.get("dataset_version"),
            service_version=service.get("version"),
            now=_parse_ts(service.get("now")),
            platforms=platforms,
            raw=dict(d),
        )


# --------------------------------------------------------------------------
# shared book/price shapes
# --------------------------------------------------------------------------

@dataclass(slots=True)
class BookTop:
    """Top-of-book snapshot. Covers both the ``book`` field on markets/legs
    (bid/ask/spread/last/day_change[/bid_size/ask_size]) and the ``top``
    field on ``/book`` orderbook responses (adds mid/mid_reliable).
    """

    bid: float | None = None
    ask: float | None = None
    spread: float | None = None
    last: float | None = None
    day_change: float | None = None
    bid_size: float | None = None
    ask_size: float | None = None
    mid: float | None = None
    mid_reliable: bool | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Self:
        return _autofill(cls, d)


# --------------------------------------------------------------------------
# /v1/markets/matched, /v1/markets/{ref}/equivalents (shared leg/cross_venue shapes)
# --------------------------------------------------------------------------

@dataclass(slots=True)
class MarketLeg:
    """One venue's side of a ``MatchedPair`` (the ``kalshi``/``polymarket`` sub-object)."""

    id: str | None = None
    question: str | None = None
    venue: str | None = None
    implied_yes: float | None = None
    book: BookTop | None = None
    volume_usd: float | None = None
    url: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Self:
        d = d if isinstance(d, dict) else {}
        book = d.get("book")
        return _autofill(cls, d, book=BookTop.from_dict(book) if isinstance(book, dict) else None)


@dataclass(slots=True)
class CrossVenue:
    """Cross-venue pricing/arb fields on a matched pair.

    In practice, live samples of ``/v1/markets/matched`` populate at most
    ``kalshi_implied`` (Polymarket-side live pricing isn't hydrated in the
    listing endpoint) — ``polymarket_implied``/``net_edge``/``spread``/
    ``executable`` are modeled per the design spec's field list even though
    not observed populated live; they default to None and are never guessed.
    """

    kalshi_implied: float | None = None
    polymarket_implied: float | None = None
    net_edge: float | None = None
    spread: float | None = None
    executable: bool | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Self:
        return _autofill(cls, d)


@dataclass(slots=True)
class MatchedPair:
    """One row of ``GET /v1/markets/matched``'s ``pairs`` array."""

    kalshi: MarketLeg | None = None
    polymarket: MarketLeg | None = None
    bet_type: str | None = None
    confidence: float | None = None
    method: str | None = None
    cross_venue: CrossVenue | None = None
    is_live: bool | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Self:
        d = d if isinstance(d, dict) else {}
        return cls(
            kalshi=MarketLeg.from_dict(d.get("kalshi") or {}),
            polymarket=MarketLeg.from_dict(d.get("polymarket") or {}),
            bet_type=d.get("bet_type"),
            confidence=d.get("confidence"),
            method=d.get("method"),
            cross_venue=CrossVenue.from_dict(d.get("cross_venue") or {}),
            is_live=d.get("is_live"),
            raw=dict(d),
        )


@dataclass(slots=True)
class MatchedPage:
    """``GET /v1/markets/matched`` — the full paginated response."""

    pairs: list[MatchedPair] = field(default_factory=list)
    total: int | None = None
    meta: dict[str, Any] | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Self:
        d = d if isinstance(d, dict) else {}
        return cls(
            pairs=[MatchedPair.from_dict(p) for p in d.get("pairs") or []],
            total=d.get("total"),
            meta=d.get("meta"),
            raw=dict(d),
        )


# ``find_divergences`` is documented as a pure convenience wrapper over
# ``matched_pairs(sort_by="net_edge")`` — same wire shape, no separate route,
# so it reuses MatchedPair's parsing rather than duplicating it.
Divergence = MatchedPair


# --------------------------------------------------------------------------
# /v1/markets/search, /v1/markets/screen, /v1/markets/{ref}/core (+ the thin
# "market" stub nested in equivalents/related/rules/history — same lenient
# parser handles both the full row and the stub, since missing fields -> None)
# --------------------------------------------------------------------------

@dataclass(slots=True)
class Market:
    """A market row as returned by search/screen/core (and the thinner
    ``market`` stubs nested in equivalents/related/rules responses).
    """

    id: str | None = None
    question: str | None = None
    venue: str | None = None
    bundle_id: str | None = None
    bundle_label: str | None = None
    status: str | None = None
    volume_usd: float | None = None
    liquidity_usd: float | None = None
    url: str | None = None
    resolution_at: datetime | None = None
    days_to_resolution: float | None = None
    implied_yes: float | None = None
    book: BookTop | None = None
    resolution: str | None = None
    resolution_status: str | None = None
    condition_id: str | None = None
    event_key: str | None = None
    is_stale: bool | None = None
    bundle_top_outcome: dict[str, Any] | None = None
    bundle_outcomes: list[Any] | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Self:
        d = d if isinstance(d, dict) else {}
        book = d.get("book")
        return _autofill(
            cls,
            d,
            resolution_at=_parse_ts(d.get("resolution_at")),
            book=BookTop.from_dict(book) if isinstance(book, dict) else None,
            # /context returns "resolution_criteria" for the same text search/screen call "resolution".
            resolution=d.get("resolution") or d.get("resolution_criteria"),
        )


@dataclass(slots=True)
class SearchPage:
    """``GET /v1/markets/search``."""

    markets: list[Market] = field(default_factory=list)
    count: int | None = None
    meta: dict[str, Any] | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Self:
        d = d if isinstance(d, dict) else {}
        return cls(
            markets=[Market.from_dict(m) for m in d.get("markets") or []],
            count=d.get("count"),
            meta=d.get("meta"),
            raw=dict(d),
        )


@dataclass(slots=True)
class ScreenPage:
    """``GET /v1/markets/screen`` — same envelope shape as SearchPage today,
    kept as a distinct type since the two routes are independently
    versioned (screen already carries screen-only meta like ``dropped_stale``).
    """

    markets: list[Market] = field(default_factory=list)
    count: int | None = None
    meta: dict[str, Any] | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Self:
        d = d if isinstance(d, dict) else {}
        return cls(
            markets=[Market.from_dict(m) for m in d.get("markets") or []],
            count=d.get("count"),
            meta=d.get("meta"),
            raw=dict(d),
        )


# --------------------------------------------------------------------------
# /v1/markets/{ref}/equivalents
# --------------------------------------------------------------------------

@dataclass(slots=True)
class Equivalent:
    """One counterpart in ``EquivalentsResult.equivalents``."""

    id: str | None = None
    venue: str | None = None
    question: str | None = None
    bet_type: str | None = None
    poly_side: str | None = None
    confidence: float | None = None
    method: str | None = None
    implied_yes: float | None = None
    book: BookTop | None = None
    volume_usd: float | None = None
    url: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Self:
        d = d if isinstance(d, dict) else {}
        book = d.get("book")
        return _autofill(cls, d, book=BookTop.from_dict(book) if isinstance(book, dict) else None)


@dataclass(slots=True)
class EquivalentsResult:
    """``GET /v1/markets/{ref}/equivalents``."""

    market: Market | None = None
    equivalents: list[Equivalent] = field(default_factory=list)
    cross_venue: CrossVenue | None = None
    meta: dict[str, Any] | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Self:
        d = d if isinstance(d, dict) else {}
        cross_venue = d.get("cross_venue")
        return cls(
            market=Market.from_dict(d.get("market") or {}),
            equivalents=[Equivalent.from_dict(e) for e in d.get("equivalents") or []],
            cross_venue=CrossVenue.from_dict(cross_venue) if isinstance(cross_venue, dict) else None,
            meta=d.get("meta"),
            raw=dict(d),
        )


# --------------------------------------------------------------------------
# /v1/markets/{ref}/related
#
# NOTE: every live probe against /related returned an empty `related: []`
# list at sample time (no correlated-but-not-equivalent pairs surfaced for
# the refs tried), so the row shape below is inferred from the server
# implementation (src/pytheum/api/markets_related.py:_build_related_item),
# not a live payload. The envelope ({market, related, meta}) *was* observed
# live and matches.
# --------------------------------------------------------------------------

@dataclass(slots=True)
class RelatedMarket:
    """One row of ``RelatedResult.related`` — a correlated-but-not-equivalent
    counterpart (different settlement band/deadline/source than the focal
    market). Field shape inferred from server source, see module note above.
    """

    id: str | None = None
    venue: str | None = None
    question: str | None = None
    relation: str | None = None
    asset: str | None = None
    date: str | None = None
    kalshi_band: str | None = None
    pm_band: str | None = None
    basis_note: str | None = None
    implied_yes: float | None = None
    book: BookTop | None = None
    volume_usd: float | None = None
    url: str | None = None
    condition_id: str | None = None
    status: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Self:
        d = d if isinstance(d, dict) else {}
        book = d.get("book")
        return _autofill(cls, d, book=BookTop.from_dict(book) if isinstance(book, dict) else None)


@dataclass(slots=True)
class RelatedResult:
    """``GET /v1/markets/{ref}/related`` (live-verified envelope shape)."""

    market: Market | None = None
    related: list[RelatedMarket] = field(default_factory=list)
    meta: dict[str, Any] | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Self:
        d = d if isinstance(d, dict) else {}
        return cls(
            market=Market.from_dict(d.get("market") or {}),
            related=[RelatedMarket.from_dict(r) for r in d.get("related") or []],
            meta=d.get("meta"),
            raw=dict(d),
        )


# --------------------------------------------------------------------------
# /v1/markets/{ref}/book
# --------------------------------------------------------------------------

@dataclass(slots=True)
class BookLevel:
    """One ``[price, size]`` level from the ``bids``/``asks`` arrays.

    ``from_dict`` accepts either the wire shape (a 2-element ``[price, size]``
    list) or an already-dict-shaped level (``{"price": ..., "size": ...}``);
    ``.raw`` is normalized to the latter either way since there's no dict to
    preserve verbatim for the list form.
    """

    price: float | None = None
    size: float | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, d: Any) -> Self:
        if isinstance(d, (list, tuple)):
            price = d[0] if len(d) > 0 else None
            size = d[1] if len(d) > 1 else None
        elif isinstance(d, dict):
            price = d.get("price")
            size = d.get("size")
        else:
            price = size = None
        return cls(price=price, size=size, raw={"price": price, "size": size})


@dataclass(slots=True)
class Orderbook:
    """``GET /v1/markets/{ref}/book``."""

    bids: list[BookLevel] = field(default_factory=list)
    asks: list[BookLevel] = field(default_factory=list)
    venue: str | None = None
    ref: str | None = None
    ts: datetime | None = None
    source: str | None = None
    top: BookTop | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Self:
        d = d if isinstance(d, dict) else {}
        top = d.get("top")
        return cls(
            bids=[BookLevel.from_dict(x) for x in d.get("bids") or []],
            asks=[BookLevel.from_dict(x) for x in d.get("asks") or []],
            venue=d.get("venue"),
            ref=d.get("ref"),
            ts=_parse_ts(d.get("ts")),
            source=d.get("source"),
            top=BookTop.from_dict(top) if isinstance(top, dict) else None,
            raw=dict(d),
        )


# --------------------------------------------------------------------------
# /v1/markets/{ref}/trades
# --------------------------------------------------------------------------

@dataclass(slots=True)
class Trade:
    """One row of ``TradesPage.trades``."""

    ts: datetime | None = None
    price: float | None = None
    size: float | None = None
    side: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Self:
        return _autofill(cls, d, ts=_parse_ts((d or {}).get("ts")))


@dataclass(slots=True)
class TradesPage:
    """``GET /v1/markets/{ref}/trades``."""

    trades: list[Trade] = field(default_factory=list)
    venue: str | None = None
    ref: str | None = None
    count: int | None = None
    source: str | None = None
    newest_trade_age_s: float | None = None
    is_stale: bool | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Self:
        d = d if isinstance(d, dict) else {}
        return cls(
            trades=[Trade.from_dict(t) for t in d.get("trades") or []],
            venue=d.get("venue"),
            ref=d.get("ref"),
            count=d.get("count"),
            source=d.get("source"),
            newest_trade_age_s=d.get("newest_trade_age_s"),
            is_stale=d.get("is_stale"),
            raw=dict(d),
        )


# --------------------------------------------------------------------------
# /v1/markets/{ref}/ohlcv
# --------------------------------------------------------------------------

@dataclass(slots=True)
class OHLCVBar:
    """One candle. Field names (``t/o/h/l/c/v``) mirror the wire shape exactly."""

    t: datetime | None = None
    o: float | None = None
    h: float | None = None
    l: float | None = None  # noqa: E741 — matches the API's own field name
    c: float | None = None
    v: float | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Self:
        return _autofill(cls, d, t=_parse_ts((d or {}).get("t")))


@dataclass(slots=True)
class OHLCVSeries:
    """``GET /v1/markets/{ref}/ohlcv``."""

    market: Market | None = None
    interval: str | None = None
    candles: list[OHLCVBar] = field(default_factory=list)
    meta: dict[str, Any] | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Self:
        d = d if isinstance(d, dict) else {}
        market = d.get("market")
        return cls(
            market=Market.from_dict(market) if isinstance(market, dict) else None,
            interval=d.get("interval"),
            candles=[OHLCVBar.from_dict(c) for c in d.get("candles") or []],
            meta=d.get("meta"),
            raw=dict(d),
        )


# --------------------------------------------------------------------------
# /v1/traders/leaderboard, /v1/traders/{wallet}
# --------------------------------------------------------------------------

@dataclass(slots=True)
class LeaderboardEntry:
    """One row of ``LeaderboardResult.traders``. ``rank`` is a string on the
    wire (e.g. "1") and kept as-is rather than silently coerced.
    """

    name: str | None = None
    address: str | None = None
    profit: float | None = None
    volume: float | None = None
    positions_value: float | None = None
    rank: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Self:
        return _autofill(cls, d)


@dataclass(slots=True)
class LeaderboardResult:
    """``GET /v1/traders/leaderboard``."""

    period: str | None = None
    traders: list[LeaderboardEntry] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Self:
        d = d if isinstance(d, dict) else {}
        return cls(
            period=d.get("period"),
            traders=[LeaderboardEntry.from_dict(t) for t in d.get("traders") or []],
            raw=dict(d),
        )


@dataclass(slots=True)
class TraderPosition:
    """One row of ``Trader.positions``."""

    market: str | None = None
    outcome: str | None = None
    size: float | None = None
    avg_price: float | None = None
    current_value: float | None = None
    profit: float | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Self:
        return _autofill(cls, d)


@dataclass(slots=True)
class Trader:
    """``GET /v1/traders/{wallet}``."""

    wallet: str | None = None
    positions: list[TraderPosition] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Self:
        d = d if isinstance(d, dict) else {}
        return cls(
            wallet=d.get("wallet"),
            positions=[TraderPosition.from_dict(p) for p in d.get("positions") or []],
            raw=dict(d),
        )


# --------------------------------------------------------------------------
# /v1/markets/{ref}/holders
# --------------------------------------------------------------------------

@dataclass(slots=True)
class Holder:
    """One row of ``HoldersPage.holders``. ``outcome`` is the raw ERC-1155
    token/outcome id string, not a human label — Polymarket-only.
    """

    address: str | None = None
    amount: float | None = None
    outcome: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Self:
        return _autofill(cls, d)


@dataclass(slots=True)
class HoldersPage:
    """``GET /v1/markets/{ref}/holders``."""

    holders: list[Holder] = field(default_factory=list)
    count: int | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Self:
        d = d if isinstance(d, dict) else {}
        return cls(
            holders=[Holder.from_dict(h) for h in d.get("holders") or []],
            count=d.get("count"),
            raw=dict(d),
        )


# --------------------------------------------------------------------------
# /v1/markets/whale-trades
# --------------------------------------------------------------------------

@dataclass(slots=True)
class WhaleTrade:
    """One row of ``WhaleTradesPage.trades``. Polymarket-only (Kalshi trades
    are anonymized) — ``market`` here is the Polymarket condition/token id,
    not a venue-prefixed ref.
    """

    ts: datetime | None = None
    market: str | None = None
    price: float | None = None
    size: float | None = None
    notional_usd: float | None = None
    side: str | None = None
    wallet: str | None = None
    pseudonym: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Self:
        return _autofill(cls, d, ts=_parse_ts((d or {}).get("ts")))


@dataclass(slots=True)
class WhaleTradesPage:
    """``GET /v1/markets/whale-trades``."""

    trades: list[WhaleTrade] = field(default_factory=list)
    count: int | None = None
    min_usd: float | None = None
    venue: str | None = None
    source: str | None = None
    note: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Self:
        d = d if isinstance(d, dict) else {}
        return cls(
            trades=[WhaleTrade.from_dict(t) for t in d.get("trades") or []],
            count=d.get("count"),
            min_usd=d.get("min_usd"),
            venue=d.get("venue"),
            source=d.get("source"),
            note=d.get("note"),
            raw=dict(d),
        )
