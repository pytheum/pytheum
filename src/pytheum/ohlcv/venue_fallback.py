"""VenueFallbackOhlcv — venue-native candle APIs (Kalshi + PM).

When the PIT archive has no coverage for a ref/range, VenueFallbackOhlcv
fetches directly from each venue's candle endpoint and normalises the result
into our shared candle dict format.

Source tag: "venue_live".
"""
from __future__ import annotations

import contextlib
import logging
from datetime import UTC, datetime
from typing import Any

from pytheum.ohlcv.provider import OhlcvProvider, OhlcvResult
from pytheum.ohlcv.resample import _INTERVALS, _KALSHI_NATIVE, resample_to_ohlcv
from pytheum.trader.cache import SingleFlightCache
from pytheum.trader.resolve import PmResolved, kalshi_ticker_from_ref, resolve_pm

logger = logging.getLogger(__name__)

__all__ = ["VenueFallbackOhlcv", "_parse_kalshi_candles"]

_TTL_RESOLVE: float = 300.0  # PM token-id resolution TTL (immutable ids)
_TTL_OHLCV: float = 60.0     # Venue candle fall-through TTL (per spec)


def _dollar(val: Any) -> float | None:
    """Convert a raw price string/value to float, returning None on failure."""
    if val is None:
        return None
    with contextlib.suppress(ValueError, TypeError):
        return float(val)
    return None


def _parse_kalshi_candles(
    body: dict[str, Any],
    interval_s: int,
) -> list[dict[str, Any]]:
    """Normalise a Kalshi ``/candlesticks`` response into our candle format.

    Kalshi item shape (from core normalizer test fixture):
        {"end_period_ts": <int>, "yes_bid": {"open_dollars": "0.45",
         "high_dollars": "0.48", "low_dollars": "0.44", "close_dollars": "0.46"},
         "volume_fp": "100"}

    Bucket start = end_period_ts - interval_s.
    """
    raw: list[dict[str, Any]] = body.get("candlesticks") or []
    out: list[dict[str, Any]] = []
    for c in raw:
        end_ts = c.get("end_period_ts")
        if end_ts is None:
            continue
        bs = datetime.fromtimestamp(end_ts - interval_s, tz=UTC)
        yb = c.get("yes_bid") or {}

        o   = _dollar(yb.get("open_dollars"))
        h   = _dollar(yb.get("high_dollars"))
        low = _dollar(yb.get("low_dollars"))
        cl  = _dollar(yb.get("close_dollars"))

        vol: float | None = None
        vol_raw = c.get("volume_fp")
        if vol_raw is not None:
            with contextlib.suppress(ValueError, TypeError):
                vol = float(vol_raw)

        out.append({
            "t": bs.isoformat().replace("+00:00", "Z"),
            "o": round(o,   4) if o   is not None else None,
            "h": round(h,   4) if h   is not None else None,
            "l": round(low, 4) if low is not None else None,
            "c": round(cl,  4) if cl  is not None else None,
            "v": vol,
        })
    return sorted(out, key=lambda x: x["t"])


class VenueFallbackOhlcv(OhlcvProvider):
    """Fetch OHLCV candles directly from each venue's native API.

    Used as the fallback leg inside PitArchiveOhlcv when the PIT archive
    has no coverage for the requested ref/range.

    Parameters
    ----------
    clients:
        TraderClients (or any object with .kalshi.rest / .polymarket.gamma + .clob).
        May be None — all fetches degrade gracefully to empty bars.
    cache_obj:
        SingleFlightCache for request coalescing. Defaults to a new instance
        per provider (production path); tests should pass an explicit cache to
        control coalescing and avoid cross-test leakage.
    """

    def __init__(
        self,
        clients: Any,
        cache_obj: SingleFlightCache | None = None,
    ) -> None:
        self._clients = clients
        self._cache: SingleFlightCache = (
            cache_obj if cache_obj is not None else SingleFlightCache()
        )

    # ── OhlcvProvider interface ───────────────────────────────────────────────

    async def available_since(self, ref: str) -> datetime | None:
        """Venue APIs do not expose a fixed start date — always None."""
        return None

    async def get_bars(
        self,
        ref: str,
        interval: str,
        since: datetime,
        until: datetime,
        limit: int,
    ) -> OhlcvResult:
        """Fetch OHLCV from the venue; returns OhlcvResult(source='venue_live')."""
        interval_s = _INTERVALS.get(interval)
        if interval_s is None:
            return OhlcvResult(bars=[], source="venue_live", partial_last_bucket=False)

        head, sep, body_ref = ref.partition(":")
        venue = head.lower() if sep else ""

        candles: list[dict[str, Any]] = []
        now = datetime.now(UTC)

        if venue == "kalshi" and self._clients is not None:
            ticker = kalshi_ticker_from_ref(ref)
            vc = await self._fetch_kalshi(ticker, interval, interval_s, since, until)
            if vc:
                candles = vc
        elif venue == "polymarket" and self._clients is not None:
            vc = await self._fetch_pm(body_ref, interval_s, since, until)
            if vc:
                candles = vc

        # Partial-bucket check.
        partial = False
        if candles:
            last_bs = datetime.fromisoformat(candles[-1]["t"].replace("Z", "+00:00"))
            partial = (last_bs.timestamp() + interval_s) > now.timestamp()

        # Clip to limit.
        if len(candles) > limit:
            candles = candles[-limit:]

        return OhlcvResult(bars=candles, source="venue_live", partial_last_bucket=partial)

    # ── Kalshi ────────────────────────────────────────────────────────────────

    async def _fetch_kalshi(
        self,
        ticker: str,
        interval: str,
        interval_s: int,
        since: datetime,
        until: datetime,
    ) -> list[dict[str, Any]] | None:
        """Fetch Kalshi OHLCV via get_historical_candlesticks. Returns None on error.

        For intervals not natively supported (5m, 15m), requests 1m data and
        aggregates using resample_to_ohlcv (close of each 1m candle as the
        resampling point — approximate but sufficient for the fall-through path).
        """
        kalshi_client = getattr(self._clients, "kalshi", None)
        if kalshi_client is None:
            return None
        rest = getattr(kalshi_client, "rest", None)
        if rest is None:
            return None

        fetch_iv = interval if interval in _KALSHI_NATIVE else "1m"
        start_ts = int(since.timestamp())
        end_ts = int(until.timestamp())
        cache_key = ("ohlcv_kalshi", ticker, fetch_iv, start_ts, end_ts)

        async def _do() -> dict[str, Any]:
            body, _ = await rest.get_historical_candlesticks(
                ticker,
                series_ticker=ticker,  # intentionally unused — audit Finding #1
                interval=fetch_iv,
                start_ts=start_ts,
                end_ts=end_ts,
            )
            return body  # type: ignore[no-any-return]

        try:
            body = await self._cache.get_or_fetch(cache_key, _TTL_OHLCV, _do)
        except Exception as exc:
            logger.warning("kalshi ohlcv fetch failed ticker=%s: %s", ticker, exc)
            return None

        native_s = _INTERVALS.get(fetch_iv, 60)
        candles_base = _parse_kalshi_candles(body, native_s)

        if fetch_iv == interval:
            return candles_base

        # Aggregate 1m → 5m / 15m via resample_to_ohlcv.
        pts = []
        for c in candles_base:
            t_str = c.get("t")
            if not t_str or c.get("c") is None:
                continue
            try:
                ts = datetime.fromisoformat(t_str.replace("Z", "+00:00"))
            except ValueError:
                continue
            pts.append({
                "ts": ts,
                "yes_price": float(c["c"]),
                "n_trades": int(c["v"]) if c.get("v") is not None else None,
            })

        if not pts:
            return []
        candles, _ = resample_to_ohlcv(pts, interval_s, since=since, until=until, limit=50_000)
        return candles

    # ── PM ─────────────────────────────────────────────────────────────────────

    async def _fetch_pm(
        self,
        ref_body: str,
        interval_s: int,
        since: datetime,
        until: datetime,
    ) -> list[dict[str, Any]] | None:
        """Fetch PM /prices-history and resample to OHLCV. Returns None on error.

        PM returns [{t: unix_s, p: float}] — a price-point series, not OHLCV
        candles. We resample it with resample_to_ohlcv so the output shape is
        identical to the archive path.
        """
        pm_client = getattr(self._clients, "polymarket", None)
        if pm_client is None:
            return None

        resolve_key = ("resolve_pm", ref_body)

        async def _resolve() -> PmResolved:
            return await resolve_pm(ref_body, gamma=pm_client.gamma)

        try:
            resolved: PmResolved = await self._cache.get_or_fetch(
                resolve_key, _TTL_RESOLVE, _resolve
            )
        except Exception as exc:
            logger.warning("pm resolve failed ref=%s: %s", ref_body, exc)
            return None

        start_ts = int(since.timestamp())
        end_ts = int(until.timestamp())
        duration_s = max(1, end_ts - start_ts)
        fidelity = min(1000, max(100, duration_s // interval_s * 2 + 10))
        cache_key = ("ohlcv_pm", resolved.token_id, start_ts, end_ts, fidelity)

        async def _fetch() -> dict[str, Any]:
            body, _ = await pm_client.clob.get_prices_history(
                resolved.token_id,
                interval="max",
                start_ts=start_ts,
                end_ts=end_ts,
                fidelity=fidelity,
            )
            return body  # type: ignore[no-any-return]

        try:
            body = await self._cache.get_or_fetch(cache_key, _TTL_OHLCV, _fetch)
        except Exception as exc:
            logger.warning("pm ohlcv fetch failed ref=%s: %s", ref_body, exc)
            return None

        pts: list[dict[str, Any]] = []
        for item in body.get("history") or []:
            t_raw, p_raw = item.get("t"), item.get("p")
            if t_raw is None or p_raw is None:
                continue
            try:
                ts = datetime.fromtimestamp(int(t_raw), tz=UTC)
                price = float(p_raw)
            except (ValueError, TypeError):
                continue
            pts.append({"ts": ts, "yes_price": price, "n_trades": None})

        if not pts:
            return []
        candles, _ = resample_to_ohlcv(pts, interval_s, since=since, until=until, limit=50_000)
        return candles
