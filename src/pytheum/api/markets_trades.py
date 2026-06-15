"""GET /v1/markets/{ref}/trades?limit=100 — recent trade tape.

Kalshi:    core KalshiRest.get_trades_page(ticker, limit=limit)
Polymarket: resolve ref → condition_id, then data.get_trades(markets=[condition_id], limit=limit)

Normalised output:
  {trades: [{ts, price, size, side}, ...], venue, ref, count, source:"live",
   newest_trade_age_s, is_stale}   # freshness on the tape (settled markets read stale)

Error degradation: any venue/network error returns
  200 with {error, detail, source:"unavailable", venue, ref}
"""
from __future__ import annotations

import logging
import time
from datetime import UTC, datetime
from typing import Any

from pytheum.api.ref_utils import normalize_ref
from pytheum.trader.cache import _TTL_TRADES, SingleFlightCache
from pytheum.trader.normalizers import normalize_kalshi_trades, normalize_pm_trades
from pytheum.trader.resolve import PmResolved, kalshi_ticker_from_ref, resolve_pm

logger = logging.getLogger(__name__)

__all__ = ["handle_market_trades"]

_cache = SingleFlightCache()
_TTL_RESOLVE: float = 300.0
# A tape whose newest trade is older than this is flagged stale (e.g. a settled
# market whose last trades fired at resolution and never refreshed). Generous so
# merely-quiet-but-live markets aren't false-flagged; the raw age is also exposed.
_TRADE_STALE_GRACE_S: float = 21600.0  # 6h


def _parse_limit(query: dict[str, str]) -> int:
    try:
        return max(1, min(int(query.get("limit", 100)), 1000))
    except (ValueError, TypeError):
        return 100


def _parse_ts_epoch(ts: Any) -> float | None:
    """Best-effort parse of a trade ts (ISO8601 string or epoch s/ms) -> epoch seconds."""
    if ts is None:
        return None
    if isinstance(ts, (int, float)):
        v = float(ts)
        return v / 1000.0 if v > 1e12 else v
    s = str(ts).strip()
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return dt.timestamp()
    except ValueError:
        pass
    try:
        v = float(s)
        return v / 1000.0 if v > 1e12 else v
    except ValueError:
        return None


def _trade_freshness(trades: list[dict[str, Any]]) -> tuple[float | None, bool]:
    """Newest-trade age (seconds) + is_stale flag — honest freshness on the 'live' tape."""
    epochs = [e for t in trades if (e := _parse_ts_epoch(t.get("ts"))) is not None]
    if not epochs:
        return None, False
    age = max(0.0, time.time() - max(epochs))
    return round(age, 1), age > _TRADE_STALE_GRACE_S


def _error_response(ref: str, venue: str, exc: BaseException) -> tuple[int, dict[str, Any]]:
    return 200, {
        "error": "venue_unavailable",
        "detail": str(exc)[:300],
        "source": "unavailable",
        "venue": venue,
        "ref": ref,
    }


async def handle_market_trades(
    ref: str,
    query: dict[str, str],
    *,
    clients: Any,
    _cache: SingleFlightCache = _cache,
) -> tuple[int, dict[str, Any]]:
    """GET /v1/markets/{ref}/trades handler."""
    limit = _parse_limit(query)
    ref_norm = normalize_ref(ref)
    head, sep, body = ref_norm.partition(":")
    venue = head.lower() if sep else ""

    cache_key = ("trades", ref_norm, limit)

    if venue == "kalshi":
        kalshi_client = getattr(clients, "kalshi", None)
        if kalshi_client is None:
            return 200, {"error": "clients_not_ready", "source": "unavailable", "ref": ref_norm}
        ticker = kalshi_ticker_from_ref(ref_norm)

        async def _fetch_kalshi() -> dict[str, Any]:
            raw_body, _env, _cursor = await kalshi_client.rest.get_trades_page(
                ticker, limit=limit
            )
            trades = normalize_kalshi_trades(raw_body, limit=limit)
            age_s, is_stale = _trade_freshness(trades)
            return {"trades": trades, "venue": "kalshi", "ref": ref_norm,
                    "count": len(trades), "source": "live",
                    "newest_trade_age_s": age_s, "is_stale": is_stale}

        try:
            result = await _cache.get_or_fetch(cache_key, _TTL_TRADES, _fetch_kalshi)
        except Exception as exc:
            logger.warning("kalshi trades fetch failed ref=%s: %s", ref_norm, exc)
            return _error_response(ref_norm, "kalshi", exc)
        return 200, result

    elif venue == "polymarket":
        pm_client = getattr(clients, "polymarket", None)
        if pm_client is None:
            return 200, {"error": "clients_not_ready", "source": "unavailable", "ref": ref_norm}

        resolve_key = ("resolve_pm", ref_norm)

        async def _resolve() -> PmResolved:
            return await resolve_pm(body, gamma=pm_client.gamma)

        try:
            resolved: PmResolved = await _cache.get_or_fetch(resolve_key, _TTL_RESOLVE, _resolve)
        except Exception as exc:
            logger.warning("pm token resolve failed ref=%s: %s", ref_norm, exc)
            return _error_response(ref_norm, "polymarket", exc)

        async def _fetch_pm() -> dict[str, Any]:
            items, _env = await pm_client.data.get_trades(
                markets=[resolved.condition_id], limit=limit
            )
            trades = normalize_pm_trades(items, limit=limit)
            age_s, is_stale = _trade_freshness(trades)
            return {"trades": trades, "venue": "polymarket", "ref": ref_norm,
                    "count": len(trades), "source": "live",
                    "newest_trade_age_s": age_s, "is_stale": is_stale}

        try:
            result = await _cache.get_or_fetch(cache_key, _TTL_TRADES, _fetch_pm)
        except Exception as exc:
            logger.warning("pm trades fetch failed ref=%s: %s", ref_norm, exc)
            return _error_response(ref_norm, "polymarket", exc)
        return 200, result

    else:
        return 200, {
            "error": "unknown_venue",
            "detail": f"ref must be venue-prefixed (kalshi:… or polymarket:…), got {ref_norm!r}",
            "ref": ref_norm,
        }
