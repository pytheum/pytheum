"""Exception taxonomy for the pytheum client.

All client-raised errors derive from :class:`PytheumError`, so callers can catch
one base. HTTP failures become :class:`APIError` subclasses carrying the status,
parsed body, and the API's ``hint`` when present; network failures become
:class:`ConnectionFailed` / :class:`PytheumTimeout`.
"""
from __future__ import annotations

from typing import Any


class PytheumError(Exception):
    """Base for every error raised by the client."""


class APIError(PytheumError):
    """Non-2xx HTTP response. Carries status, parsed body, and the API hint."""

    def __init__(self, status: int, body: Any = None, *, hint: str | None = None) -> None:
        self.status = status
        self.body = body
        # Surface the API's own {error, hint} when the body is a dict.
        if hint is None and isinstance(body, dict):
            hint = body.get("hint") or body.get("detail")
        self.hint = hint
        msg = f"HTTP {status}"
        detail = None
        if isinstance(body, dict):
            detail = body.get("error") or body.get("detail")
        if detail:
            msg += f": {detail}"
        if hint and hint != detail:
            msg += f" (hint: {hint})"
        super().__init__(msg)


class RateLimitError(APIError):
    """HTTP 429. ``retry_after`` is the server's requested wait in seconds, if given."""

    def __init__(self, status: int, body: Any = None, *, retry_after: float | None = None,
                 hint: str | None = None) -> None:
        self.retry_after = retry_after
        super().__init__(status, body, hint=hint)


class NotFoundError(APIError):
    """HTTP 404 — market ref / resource does not exist."""


class AuthError(APIError):
    """HTTP 401 / 403."""


class ServerError(APIError):
    """HTTP 5xx (after retries are exhausted)."""


class PytheumTimeout(PytheumError):
    """A request timed out (connect/read/write/pool) after retries."""


class ConnectionFailed(PytheumError):
    """The request never reached the server (DNS/connect) after retries."""


def error_for_status(status: int, body: Any, *, retry_after: float | None = None) -> APIError:
    """Map an HTTP status to the right :class:`APIError` subclass."""
    if status == 429:
        return RateLimitError(status, body, retry_after=retry_after)
    if status == 404:
        return NotFoundError(status, body)
    if status in (401, 403):
        return AuthError(status, body)
    if status >= 500:
        return ServerError(status, body)
    return APIError(status, body)
