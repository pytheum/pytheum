"""The concurrency + resilience engine shared by the sync and async clients.

This is the load-bearing part: connection pooling, a client-side concurrency
governor, and a bounded retry policy (exponential backoff + full jitter, with
429 ``Retry-After`` honored exactly). Both transports execute a venue-agnostic
:class:`RequestSpec` and return the parsed JSON body (the REST API returns direct
payloads — no envelope), raising the right :class:`APIError` on failure.
"""
from __future__ import annotations

import asyncio
import email.utils
import random
import threading
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

from .errors import (
    ConnectionFailed,
    PytheumTimeout,
    error_for_status,
)

DEFAULT_BASE_URL = "https://api.pytheum.com"
_RETRY_STATUS = frozenset({429, 502, 503, 504})
_TIMEOUT_EXC = (httpx.ConnectTimeout, httpx.ReadTimeout, httpx.WriteTimeout, httpx.PoolTimeout)
_CONNECT_EXC = (httpx.ConnectError,)


@dataclass(frozen=True, slots=True)
class RequestSpec:
    """A single HTTP call: method, path (already ref-substituted), query params."""

    method: str
    path: str
    params: dict[str, Any] = field(default_factory=dict)

    def clean_params(self) -> dict[str, Any]:
        """Drop None-valued params; coerce bools to lowercase strings (API convention)."""
        out: dict[str, Any] = {}
        for k, v in self.params.items():
            if v is None:
                continue
            out[k] = "true" if v is True else "false" if v is False else v
        return out


@dataclass(frozen=True, slots=True)
class RetryConfig:
    """Bounded exponential-backoff-with-jitter retry policy."""

    max_retries: int = 3
    base: float = 0.25       # first backoff (s)
    cap: float = 8.0         # max single backoff (s)
    respect_retry_after: bool = True
    max_retry_after: float = 30.0  # never sleep longer than this on a Retry-After

    def backoff(self, attempt: int) -> float:
        """Full-jitter backoff for a 0-indexed retry attempt."""
        ceil = min(self.cap, self.base * (2 ** attempt))
        return random.uniform(0, ceil)


def parse_retry_after(value: str | None) -> float | None:
    """Parse a ``Retry-After`` header (delta-seconds or HTTP-date) → seconds."""
    if not value:
        return None
    value = value.strip()
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    try:
        dt = email.utils.parsedate_to_datetime(value)
    except (ValueError, TypeError):
        # Py3.10+ raises (rather than returns None) on an unparseable HTTP-date;
        # a malformed Retry-After must fall back to computed backoff, never crash.
        return None
    if dt is None:
        return None
    import datetime as _dt
    now = _dt.datetime.now(_dt.UTC)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_dt.UTC)
    return max(0.0, (dt - now).total_seconds())


def _parse_body(resp: httpx.Response) -> Any:
    try:
        return resp.json()
    except Exception:
        return resp.text or None


def _sleep_for(resp: httpx.Response | None, exc: Exception | None, attempt: int,
               retry: RetryConfig) -> float:
    """How long to wait before the next attempt."""
    if resp is not None and resp.status_code == 429 and retry.respect_retry_after:
        ra = parse_retry_after(resp.headers.get("retry-after"))
        if ra is not None:
            return min(ra, retry.max_retry_after)
    return retry.backoff(attempt)


def _should_retry(resp: httpx.Response | None, exc: Exception | None) -> bool:
    if exc is not None:
        return isinstance(exc, _TIMEOUT_EXC + _CONNECT_EXC)
    return resp is not None and resp.status_code in _RETRY_STATUS


def _raise(resp: httpx.Response) -> None:
    body = _parse_body(resp)
    ra = parse_retry_after(resp.headers.get("retry-after")) if resp.status_code == 429 else None
    raise error_for_status(resp.status_code, body, retry_after=ra)


def _wrap_network_exc(exc: Exception) -> Exception:
    if isinstance(exc, _TIMEOUT_EXC):
        return PytheumTimeout(str(exc) or exc.__class__.__name__)
    if isinstance(exc, _CONNECT_EXC):
        return ConnectionFailed(str(exc) or exc.__class__.__name__)
    return exc


def build_limits(max_connections: int, max_keepalive: int) -> httpx.Limits:
    return httpx.Limits(max_connections=max_connections,
                        max_keepalive_connections=max_keepalive)


def build_timeout(connect: float, read: float, write: float, pool: float) -> httpx.Timeout:
    return httpx.Timeout(connect=connect, read=read, write=write, pool=pool)


class AsyncTransport:
    """Async execution of :class:`RequestSpec` with pooling + governor + retries."""

    def __init__(self, *, base_url: str, headers: dict[str, str],
                 limits: httpx.Limits, timeout: httpx.Timeout,
                 max_concurrency: int, retry: RetryConfig,
                 http2: bool = False) -> None:
        self._client = httpx.AsyncClient(base_url=base_url, headers=headers,
                                         limits=limits, timeout=timeout, http2=http2)
        self._sem = asyncio.Semaphore(max_concurrency)
        self._retry = retry

    async def request(self, spec: RequestSpec) -> Any:
        attempt = 0
        while True:
            resp: httpx.Response | None = None
            exc: Exception | None = None
            async with self._sem:  # governor: bound in-flight requests
                try:
                    resp = await self._client.request(spec.method, spec.path,
                                                      params=spec.clean_params())
                except Exception as e:  # noqa: BLE001 — classified below
                    exc = e
            if exc is None and resp is not None and resp.is_success:
                return _parse_body(resp)
            if attempt < self._retry.max_retries and _should_retry(resp, exc):
                await asyncio.sleep(_sleep_for(resp, exc, attempt, self._retry))
                attempt += 1
                continue
            if exc is not None:
                raise _wrap_network_exc(exc)
            assert resp is not None
            _raise(resp)

    async def aclose(self) -> None:
        await self._client.aclose()


class SyncTransport:
    """Sync execution of :class:`RequestSpec` — a real httpx.Client, not asyncio-wrapped."""

    def __init__(self, *, base_url: str, headers: dict[str, str],
                 limits: httpx.Limits, timeout: httpx.Timeout,
                 max_concurrency: int, retry: RetryConfig,
                 http2: bool = False) -> None:
        self._client = httpx.Client(base_url=base_url, headers=headers,
                                    limits=limits, timeout=timeout, http2=http2)
        self._sem = threading.BoundedSemaphore(max_concurrency)
        self._retry = retry

    def request(self, spec: RequestSpec) -> Any:
        attempt = 0
        while True:
            resp: httpx.Response | None = None
            exc: Exception | None = None
            with self._sem:
                try:
                    resp = self._client.request(spec.method, spec.path,
                                                params=spec.clean_params())
                except Exception as e:  # noqa: BLE001
                    exc = e
            if exc is None and resp is not None and resp.is_success:
                return _parse_body(resp)
            if attempt < self._retry.max_retries and _should_retry(resp, exc):
                time.sleep(_sleep_for(resp, exc, attempt, self._retry))
                attempt += 1
                continue
            if exc is not None:
                raise _wrap_network_exc(exc)
            assert resp is not None
            _raise(resp)

    def close(self) -> None:
        self._client.close()
