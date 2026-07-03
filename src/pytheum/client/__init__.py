"""Pytheum Python SDK — a typed, concurrency-optimized client over the REST API.

    from pytheum.client import Client, AsyncClient

The async client is primary (throughput); a real synchronous ``Client`` mirrors it.
Both pool connections, bound in-flight requests with a concurrency governor, and
retry transient failures (timeouts, 429 with ``Retry-After``, 5xx) transparently.
"""
from __future__ import annotations

from . import models
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

# Methods return the parsed JSON payload (dict/list) by default — forward-compatible
# and zero-surprise. For typed access, wrap a payload in a model from `pytheum.client.models`:
#     from pytheum.client import Client, models
#     st = models.Status.from_dict(Client().status())   # st.equivalence_pairs_loaded

__all__ = [
    "AsyncClient",
    "Client",
    "models",
    "PytheumError",
    "APIError",
    "RateLimitError",
    "NotFoundError",
    "AuthError",
    "ServerError",
    "PytheumTimeout",
    "ConnectionFailed",
]
