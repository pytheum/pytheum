"""Pytheum Python SDK — a typed, concurrency-optimized client over the REST API.

    from pytheum.client import Client, AsyncClient

The async client is primary (throughput); a real synchronous ``Client`` mirrors it.
Both pool connections, bound in-flight requests with a concurrency governor, and
retry transient failures (timeouts, 429 with ``Retry-After``, 5xx) transparently.
"""
from __future__ import annotations

from .client import AsyncClient, Client
from .errors import (
    APIError,
    AuthError,
    ConnectionFailed,
    NotFoundError,
    PytheumError,
    PytheumTimeout,
    RateLimitError,
    ServerError,
)

__all__ = [
    "AsyncClient",
    "Client",
    "PytheumError",
    "APIError",
    "RateLimitError",
    "NotFoundError",
    "AuthError",
    "ServerError",
    "PytheumTimeout",
    "ConnectionFailed",
]
