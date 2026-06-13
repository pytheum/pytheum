"""VenueStreamManager — bridges pytheum-core MarketSession to public WS clients.

ONE shared MarketSession is lazily started on first subscription and torn down on
server stop.  Each WS client holds a ref-counted market subscription; the session-
level venue subscribe/unsubscribe fires only on the 0→1 and 1→0 transitions.

Hard cap: MAX_MARKETS (200) unique market keys.  Subscribing beyond the cap causes
a ``venue_warning`` event with code ``MARKET_CAP`` and an error entry in the
subscribe reply.

Degrade path: if core clients are unavailable (TraderClients.ready is False), the
manager logs a warning, includes a ``venue_warning`` event in each subscribe reply,
and skips the underlying MarketSession subscription.

Wire format (JSON):
  venue_book  → {"event":"venue_book", "market":"kalshi:TICKER", "venue":"kalshi",
                  "ts": ISO8601, "bids":[[p,s]×≤10], "asks":[[p,s]×≤10]}
  venue_trade → {"event":"venue_trade", "market":"kalshi:TICKER", "venue":"kalshi",
                  "ts": ISO8601, "price":float, "size":float, "side":"buy"|"sell"|null}
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)

VENUE_EVENTS = frozenset({"venue_book", "venue_trade"})
MAX_MARKETS = 200


# ---------------------------------------------------------------------------
# Key helpers
# ---------------------------------------------------------------------------

def _parse_market_key(key: str) -> tuple[str, str] | None:
    """Parse ``"kalshi:TICKER"`` or ``"polymarket:TOKEN_ID"`` → (venue, value).

    Returns None for unrecognised format.
    """
    if key.startswith("kalshi:"):
        value = key[len("kalshi:"):]
        return ("kalshi", value) if value else None
    if key.startswith("polymarket:"):
        value = key[len("polymarket:"):]
        return ("polymarket", value) if value else None
    return None


def _make_market_ref(venue_str: str, value: str) -> Any | None:
    """Build a MarketRef.  Returns None on import failure (core not installed)."""
    try:
        from pytheum_core.data.models import Venue
        from pytheum_core.data.refs import MarketRef, RefType

        venue = Venue.KALSHI if venue_str == "kalshi" else Venue.POLYMARKET
        ref_type = (
            RefType.KALSHI_TICKER if venue_str == "kalshi"
            else RefType.POLYMARKET_TOKEN_ID
        )
        return MarketRef(venue=venue, ref_type=ref_type, value=value)
    except Exception:
        logger.debug("venue_stream: could not build MarketRef for %s:%s", venue_str, value)
        return None


# ---------------------------------------------------------------------------
# Null persistence — discard all repository writes
# ---------------------------------------------------------------------------

class _NullRepository:
    """No-op repository that discards all persistence calls.

    ``record_raw_ws`` and ``record_raw_rest`` return 0 (an int raw_id that
    satisfies FK constraints in the stream service call chain even though
    nothing is actually stored).  Everything else is auto-stubbed via
    ``__getattr__``.
    """

    def record_raw_ws(self, **kwargs: Any) -> int:  # noqa: ARG002
        return 0

    def record_raw_rest(self, **kwargs: Any) -> int:  # noqa: ARG002
        return 0

    def __getattr__(self, name: str) -> Callable[..., None]:
        def _noop(*args: Any, **kwargs: Any) -> None:  # noqa: ARG001
            return None
        return _noop


# ---------------------------------------------------------------------------
# Session factory (production path)
# ---------------------------------------------------------------------------

def _build_session(*, kalshi_client: Any, polymarket_client: Any) -> Any:
    """Construct a MarketSession with no-op persistence and empty initial refs.

    All markets are added dynamically via ``add_refs`` / ``remove_refs``.
    ``condition_ids_by_token=None`` lets the session REST-resolve Polymarket
    token → condition-id lazily in ``add_refs``.
    """
    from pytheum_core.services.fetch import KalshiFetchService
    from pytheum_core.services.kalshi_stream import KalshiStreamService
    from pytheum_core.services.market_session import MarketSession
    from pytheum_core.services.polymarket_fetch import PolymarketFetchService
    from pytheum_core.services.polymarket_stream import PolymarketStreamService

    repo = _NullRepository()
    return MarketSession(
        refs=[],
        kalshi_fetch=KalshiFetchService(client=kalshi_client, repository=repo),
        kalshi_stream=KalshiStreamService(repository=repo),
        polymarket_fetch=PolymarketFetchService(client=polymarket_client, repository=repo),
        polymarket_stream=PolymarketStreamService(repository=repo),
        kalshi_client=kalshi_client,
        polymarket_client=polymarket_client,
        repository=repo,
        condition_ids_by_token=None,  # REST-resolved in add_refs
    )


# ---------------------------------------------------------------------------
# Normalization
# ---------------------------------------------------------------------------

def _normalize_book(key: str, venue_str: str, book: Any, ts: Any) -> str:
    """Serialize an OrderBook to a ``venue_book`` JSON string."""
    return json.dumps({
        "event": "venue_book",
        "market": key,
        "venue": venue_str,
        "ts": ts.isoformat(),
        "bids": [[float(p), float(s)] for p, s in book.bids[:10]],
        "asks": [[float(p), float(s)] for p, s in book.asks[:10]],
    })


def _normalize_trade(key: str, venue_str: str, trade: Any, ts: Any) -> str:
    """Serialize a Trade to a ``venue_trade`` JSON string."""
    return json.dumps({
        "event": "venue_trade",
        "market": key,
        "venue": venue_str,
        "ts": ts.isoformat(),
        "price": float(trade.price),
        "size": float(trade.size),
        "side": trade.side,
    })


def _warn_json(code: str, market: str, message: str) -> str:
    return json.dumps({
        "event": "venue_warning",
        "code": code,
        "market": market,
        "message": message,
    })


# ---------------------------------------------------------------------------
# Manager
# ---------------------------------------------------------------------------

class VenueStreamManager:
    """Bridge between the shared MarketSession and multiple WS client callbacks.

    Thread-safety: designed for single-thread asyncio.  All mutations happen on
    the event loop; ``asyncio.Lock`` guards only the session-creation critical
    section (prevents double-start on concurrent first-subscribe).

    State maps (no locking required for dicts in asyncio):
      _market_refs          market_key → MarketRef (present iff session-subscribed)
      _market_listeners     market_key → {listener_ids} (any event_type)
      _market_event_lids    (market_key, event_type) → {listener_ids}
      _listener_markets     listener_id → {market_keys} (any event_type)
      _listeners            listener_id → callback(json_str)
    """

    MAX_MARKETS: int = MAX_MARKETS

    def __init__(
        self,
        trader: Any,
        *,
        _session_factory: Callable[[], Any] | None = None,
    ) -> None:
        self._trader = trader
        self._session_factory = _session_factory

        self._session: Any = None
        self._fanout_task: asyncio.Task[None] | None = None
        self._session_start_lock: asyncio.Lock = asyncio.Lock()

        self._market_refs: dict[str, Any] = {}
        self._market_listeners: dict[str, set[int]] = {}
        self._market_event_lids: dict[tuple[str, str], set[int]] = {}
        self._listener_markets: dict[int, set[str]] = {}
        self._listeners: dict[int, Callable[[str], None]] = {}

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def subscribe(
        self,
        listener_id: int,
        market_keys: list[str],
        event_type: str,
        callback: Callable[[str], None],
    ) -> dict[str, Any]:
        """Subscribe *listener_id* to *market_keys* for *event_type*.

        Returns ``{"subscribed": [...], "errors": [...]}``.  Errors include
        INVALID_FORMAT, MARKET_CAP, and BUILD_FAILED entries; successfully
        subscribed keys appear in ``"subscribed"``.

        Idempotent for the same (listener, market, event_type) triple.
        """
        self._listeners[listener_id] = callback
        ok: list[str] = []
        errors: list[dict[str, str]] = []

        for key in market_keys:
            parsed = _parse_market_key(key)
            if parsed is None:
                errors.append({
                    "key": key, "error": "INVALID_FORMAT",
                    "message": (
                        f"expected kalshi:<ticker> or polymarket:<token_id>; got {key!r}"
                    ),
                })
                continue

            venue_str, value = parsed

            # Cap check — only reject genuinely new markets
            if key not in self._market_refs and len(self._market_refs) >= self.MAX_MARKETS:
                msg = f"market cap of {self.MAX_MARKETS} reached"
                errors.append({"key": key, "error": "MARKET_CAP", "message": msg})
                # Also push a live warning event so the client can display it
                try:
                    callback(_warn_json("MARKET_CAP", key, msg))
                except Exception:
                    pass
                continue

            ref = _make_market_ref(venue_str, value)
            if ref is None:
                errors.append({
                    "key": key, "error": "BUILD_FAILED",
                    "message": "could not build MarketRef (pytheum-core unavailable?)",
                })
                try:
                    callback(_warn_json("BUILD_FAILED", key, "core unavailable"))
                except Exception:
                    pass
                continue

            # Register market ref first so cap accounting is consistent
            is_new_market = key not in self._market_refs
            self._market_refs[key] = ref

            # Fanout indices
            self._market_event_lids.setdefault((key, event_type), set()).add(listener_id)
            prev_market_listeners = len(self._market_listeners.get(key, set()))
            self._market_listeners.setdefault(key, set()).add(listener_id)
            self._listener_markets.setdefault(listener_id, set()).add(key)

            # If this is the first listener for this market, subscribe at session level
            if is_new_market or prev_market_listeners == 0:
                session = await self._ensure_session()
                if session is not None:
                    try:
                        await session.add_refs([ref])
                    except Exception:
                        logger.exception(
                            "venue_stream: add_refs failed for %s", key
                        )
                        warn = "failed to subscribe to venue (session error)"
                        try:
                            callback(_warn_json("SESSION_ERROR", key, warn))
                        except Exception:
                            pass
                else:
                    warn = "venue clients unavailable; market subscription deferred"
                    logger.warning("venue_stream: session unavailable for %s", key)
                    try:
                        callback(_warn_json("VENUE_UNAVAILABLE", key, warn))
                    except Exception:
                        pass

            ok.append(key)

        return {"subscribed": ok, "errors": errors}

    async def unsubscribe(
        self,
        listener_id: int,
        market_keys: list[str],
        event_type: str,
    ) -> dict[str, Any]:
        """Unsubscribe *listener_id* from *market_keys* for *event_type*.

        When a listener's last (market, event_type) subscription for a given
        market is removed, the underlying session market is unsubscribed too.
        """
        removed: list[str] = []

        for key in market_keys:
            self._market_event_lids.get((key, event_type), set()).discard(listener_id)

            # Still interested in this market under any event type?
            still_interested = any(
                listener_id in self._market_event_lids.get((key, et), set())
                for et in VENUE_EVENTS
            )

            if not still_interested:
                self._market_listeners.get(key, set()).discard(listener_id)
                self._listener_markets.get(listener_id, set()).discard(key)

                if not self._market_listeners.get(key):
                    await self._session_remove(key)

            removed.append(key)

        return {"unsubscribed": removed}

    async def unregister_listener(self, listener_id: int) -> None:
        """Remove ALL subscriptions for *listener_id* (called on WS disconnect).

        Decrements per-market ref-counts and unsubscribes from the session for
        any market whose last listener is this one.
        """
        market_keys = list(self._listener_markets.pop(listener_id, set()))
        self._listeners.pop(listener_id, None)

        for key in market_keys:
            for et in VENUE_EVENTS:
                self._market_event_lids.get((key, et), set()).discard(listener_id)
            self._market_listeners.get(key, set()).discard(listener_id)

            if not self._market_listeners.get(key):
                await self._session_remove(key)

    async def stop(self) -> None:
        """Cancel the fanout task and close the session."""
        if self._fanout_task is not None:
            self._fanout_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._fanout_task
        if self._session is not None:
            with contextlib.suppress(Exception):
                await self._session.aclose()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _session_remove(self, key: str) -> None:
        """Unsubscribe *key* from the session and drop it from state maps."""
        ref = self._market_refs.pop(key, None)
        self._market_listeners.pop(key, None)
        for et in VENUE_EVENTS:
            self._market_event_lids.pop((key, et), None)

        if ref is not None and self._session is not None:
            try:
                await self._session.remove_refs([ref])
            except Exception:
                logger.exception("venue_stream: remove_refs failed for %s", key)

    async def _ensure_session(self) -> Any | None:
        """Lazy-start the MarketSession exactly once."""
        if self._session is not None:
            return self._session

        async with self._session_start_lock:
            if self._session is not None:
                return self._session

            try:
                if self._session_factory is not None:
                    session = self._session_factory()
                elif getattr(self._trader, "ready", False):
                    session = _build_session(
                        kalshi_client=self._trader.kalshi,
                        polymarket_client=self._trader.polymarket,
                    )
                else:
                    logger.warning(
                        "venue_stream: trader clients not ready; "
                        "MarketSession not started"
                    )
                    return None

                await session.start()
            except Exception:
                logger.exception("venue_stream: MarketSession.start() failed")
                return None

            self._session = session
            self._fanout_task = asyncio.create_task(
                self._fanout_loop(session), name="venue-stream-fanout"
            )
            return session

    async def _fanout_loop(self, session: Any) -> None:
        """Consume SessionEvents from the shared MarketSession and dispatch them."""
        try:
            async for event in session.events():
                self._dispatch_event(event)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("venue_stream: fanout loop crashed")

    def _dispatch_event(self, event: Any) -> None:
        """Normalize one SessionEvent and push JSON to matching listener callbacks.

        Synchronous — never awaits.  Safe to call inside the fanout task.
        """
        try:
            from pytheum_core.services.session_events import (
                BookResetUpdate,
                OrderBookUpdate,
                TradeUpdate,
            )
        except ImportError:
            return

        payload = event.payload
        ts = event.received_ts

        if isinstance(payload, (OrderBookUpdate, BookResetUpdate)):
            ref = payload.ref
            book = payload.book
            key = f"{ref.venue.value}:{ref.value}"
            msg = _normalize_book(key, ref.venue.value, book, ts)
            for lid in list(self._market_event_lids.get((key, "venue_book"), set())):
                self._deliver(lid, msg)

        elif isinstance(payload, TradeUpdate):
            ref = payload.ref
            trade = payload.trade
            key = f"{ref.venue.value}:{ref.value}"
            msg = _normalize_trade(key, ref.venue.value, trade, ts)
            for lid in list(self._market_event_lids.get((key, "venue_trade"), set())):
                self._deliver(lid, msg)

    def _deliver(self, listener_id: int, msg: str) -> None:
        cb = self._listeners.get(listener_id)
        if cb is not None:
            try:
                cb(msg)
            except Exception:
                logger.exception(
                    "venue_stream: callback failed lid=%d", listener_id
                )
