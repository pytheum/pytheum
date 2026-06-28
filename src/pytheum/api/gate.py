"""HTTP API edge gate: API-key auth + per-client token-bucket rate limiting.

This is the pre-public-launch gate that sits in front of :class:`RouterApp`
(``pytheum.routing``) as a thin ASGI wrapper, *before* route dispatch.  It
mirrors the proven per-IP token-bucket already shipped on the MCP connector
(``pytheum.mcp.server``) but adds:

* **API-key auth** — config-flagged (``PYTHEUM_REQUIRE_API_KEY``, default OFF).
  Keys come from ``PYTHEUM_API_KEYS`` (comma-separated) and are presented via
  ``X-API-Key`` or ``Authorization: Bearer <key>``.  A small allowlist of
  *keyless* paths (``/v1/status``, ``/healthz``, ``/llms.txt``) and all OPTIONS
  preflight requests bypass auth — they stay keyless exactly as before.
* **Per-client rate limiting** — token bucket keyed by API key when present,
  else source IP.  Returns 429 + ``Retry-After`` when exceeded.

Design constraints (the load test ran 248 RPS through this path):

* The hot path is two dict lookups + arithmetic — no locks (asyncio is
  single-threaded between awaits), no allocation per allowed request.
* State is per-process (the service runs single-process; see the MCP server
  module docstring for why).  Multi-worker deploys would need a shared store.

When ``require_api_key`` is False AND ``rate_limit_per_min`` is 0 the gate is a
zero-cost pass-through (``maybe_wrap`` returns the inner app unchanged), so the
current deployment behaves identically to before.
"""
from __future__ import annotations

import json
import time
from typing import Any

from pytheum.config import ServeConfig

__all__ = ["ApiGate", "maybe_wrap"]

# Paths that never require a key and are never rate-limited.  /v1/status is
# documented "keyless" (load-balancer health + agent situational awareness);
# /healthz + /llms.txt are infra/discovery endpoints served by RouterApp
# itself before dispatch.
_KEYLESS_PATHS: frozenset[str] = frozenset({
    "/v1/status",
    "/v1/metrics",
    "/v1/about",
    "/v1/guide",
    "/healthz",
    "/llms.txt",
})


def _parse_keys(raw: str) -> frozenset[str]:
    """Split a comma-separated key string into a set of non-empty keys."""
    return frozenset(k.strip() for k in raw.split(",") if k.strip())


def _is_keyless_path(path: str) -> bool:
    return path.rstrip("/") in _KEYLESS_PATHS or path.rstrip("/") == ""


def _header(headers: list[tuple[bytes, bytes]], name: bytes) -> str | None:
    """Return the first value of header *name* (lower-case bytes), or None."""
    for k, v in headers:
        if k.lower() == name:
            return v.decode("latin-1")
    return None


def _present_key(headers: list[tuple[bytes, bytes]]) -> str | None:
    """Extract the client key from X-API-Key or 'Authorization: Bearer <k>'."""
    xkey = _header(headers, b"x-api-key")
    if xkey:
        return xkey.strip()
    auth = _header(headers, b"authorization")
    if auth and auth[:7].lower() == "bearer ":
        return auth[7:].strip() or None
    return None


def _client_ip(scope: dict[str, Any]) -> str:
    """Best-effort client IP: X-Forwarded-For first hop (Caddy), else peer."""
    headers: list[tuple[bytes, bytes]] = scope.get("headers", [])
    xff = _header(headers, b"x-forwarded-for")
    if xff:
        return xff.split(",")[0].strip()
    client = scope.get("client")
    return str(client[0]) if client else "unknown"


class _Bucket:
    """Mutable token-bucket cell.  Refills continuously at ``rate`` tokens/s."""

    __slots__ = ("last", "tokens")

    def __init__(self, tokens: float, last: float) -> None:
        self.tokens = tokens
        self.last = last


class ApiGate:
    """ASGI gate enforcing API-key auth + per-client token-bucket rate limit.

    Wraps an inner ASGI app (``RouterApp``).  Lifespan and non-HTTP scopes pass
    straight through.  HTTP requests are checked in order: keyless bypass →
    auth → rate limit → inner app.
    """

    def __init__(
        self,
        inner: Any,
        *,
        require_api_key: bool,
        api_keys: frozenset[str],
        rate_per_min: float,
        burst: float,
    ) -> None:
        self._inner = inner
        self._require_api_key = require_api_key
        self._api_keys = api_keys
        self._rate_per_sec = rate_per_min / 60.0
        self._burst = burst
        self._rate_enabled = rate_per_min > 0
        self._buckets: dict[str, _Bucket] = {}

    # -- rate limiting ----------------------------------------------------- #

    def _allow(self, client_id: str) -> bool:
        """Token-bucket admission for *client_id*.  True = allowed."""
        now = time.monotonic()
        b = self._buckets.get(client_id)
        if b is None:
            # First request from this client — full bucket minus this token.
            if len(self._buckets) > 50_000:
                self._prune(now)
            self._buckets[client_id] = _Bucket(self._burst - 1.0, now)
            return True
        b.tokens = min(self._burst, b.tokens + (now - b.last) * self._rate_per_sec)
        b.last = now
        if b.tokens < 1.0:
            return False
        b.tokens -= 1.0
        return True

    def _prune(self, now: float) -> None:
        """Evict buckets that have fully refilled (state == fresh entry)."""
        refill_s = self._burst / self._rate_per_sec if self._rate_per_sec else 0.0
        stale = [k for k, v in self._buckets.items() if now - v.last > refill_s]
        for k in stale:
            del self._buckets[k]
        if len(self._buckets) > 50_000:
            by_age = sorted(self._buckets, key=lambda k: self._buckets[k].last)
            for k in by_age[: len(self._buckets) - 50_000]:
                del self._buckets[k]

    # -- ASGI -------------------------------------------------------------- #

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self._inner(scope, receive, send)
            return

        method = scope.get("method", "GET").upper()
        path: str = scope.get("path", "")
        headers: list[tuple[bytes, bytes]] = scope.get("headers", [])

        # OPTIONS preflight + keyless allowlist always bypass auth.
        keyless = method == "OPTIONS" or _is_keyless_path(path)

        client_key = _present_key(headers)

        auth_required = self._require_api_key and not keyless
        if auth_required and (not self._api_keys or client_key not in self._api_keys):
            await self._reject(
                send, 401, "unauthorized",
                "missing or invalid API key — supply X-API-Key or "
                "'Authorization: Bearer <key>'",
            )
            return

        # Rate-limit identity: the key when one is presented (even on keyless
        # paths), else the source IP.  Keyless infra probes (/healthz) are not
        # rate-limited so health checks never trip the limiter.
        if self._rate_enabled and not keyless:
            client_id = (
                f"key:{client_key}" if client_key else f"ip:{_client_ip(scope)}"
            )
            if not self._allow(client_id):
                await self._reject(
                    send, 429, "rate_limited",
                    "too many requests — slow down and retry",
                    retry_after=1,
                )
                return

        await self._inner(scope, receive, send)

    @staticmethod
    async def _reject(
        send: Any,
        status: int,
        error: str,
        detail: str,
        *,
        retry_after: int | None = None,
    ) -> None:
        body = json.dumps({"error": error, "detail": detail}).encode()
        headers = [(b"content-type", b"application/json")]
        if retry_after is not None:
            headers.append((b"retry-after", str(retry_after).encode()))
        await send({"type": "http.response.start", "status": status, "headers": headers})
        await send({"type": "http.response.body", "body": body})


def maybe_wrap(inner: Any, cfg: ServeConfig | None = None) -> Any:
    """Wrap *inner* in :class:`ApiGate` when auth or rate limiting is enabled.

    Returns *inner* unchanged when both auth (``require_api_key`` False) and
    rate limiting (``rate_limit_per_min`` == 0) are off — a true zero-cost
    pass-through so the default deployment is byte-for-byte unaffected.
    """
    cfg = cfg or ServeConfig()
    if not cfg.require_api_key and cfg.rate_limit_per_min <= 0:
        return inner
    return ApiGate(
        inner,
        require_api_key=cfg.require_api_key,
        api_keys=_parse_keys(cfg.api_keys),
        rate_per_min=float(cfg.rate_limit_per_min),
        burst=float(cfg.rate_limit_burst),
    )
