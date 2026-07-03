"""Unit suite for the pytheum SDK transport engine (src/pytheum/client/_transport.py).

Covers both ``AsyncTransport`` and ``SyncTransport`` executing a ``RequestSpec``
against an in-process ``httpx.MockTransport`` (no network): happy path / non-JSON
bodies, 429 retry + Retry-After honoring, 5xx retry-vs-no-retry, 4xx error mapping
(no retry), network-exception mapping, ``RequestSpec.clean_params``,
``RetryConfig.backoff`` bounds, the concurrency governor, and clean shutdown.

Transports are built via the real constructors (so ``Limits``/``Timeout``/governor/
retry wiring is exercised as written), then their internal ``httpx`` client is
swapped for one backed by ``httpx.MockTransport`` — the documented, verified way to
inject a fake network boundary without touching ``_transport.py`` itself.
"""
from __future__ import annotations

import asyncio
import email.utils
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
import pytest

from pytheum.client import _transport as transport_mod
from pytheum.client._transport import (
    AsyncTransport,
    RequestSpec,
    RetryConfig,
    SyncTransport,
    _should_retry,
    build_limits,
    build_timeout,
    parse_retry_after,
)
from pytheum.client.errors import (
    APIError,
    AuthError,
    ConnectionFailed,
    NotFoundError,
    PytheumTimeout,
    RateLimitError,
    ServerError,
)

# --------------------------------------------------------------------------
# Shared fixtures / helpers
# --------------------------------------------------------------------------

BASE_URL = "https://transport-test.example"

# Tiny backoff so retry-exhaustion tests run fast without needing to fake time
# (only the explicit Retry-After-honoring tests need to monkeypatch sleep).
FAST_RETRY = RetryConfig(max_retries=3, base=0.001, cap=0.01)


def _limits() -> httpx.Limits:
    return build_limits(max_connections=50, max_keepalive=10)


def _timeout() -> httpx.Timeout:
    return build_timeout(connect=1.0, read=1.0, write=1.0, pool=1.0)


def spec(method: str = "GET", path: str = "/thing", **params: Any) -> RequestSpec:
    return RequestSpec(method=method, path=path, params=params)


def resp(
    status: int,
    *,
    json_body: Any = None,
    text: str | None = None,
    headers: dict[str, str] | None = None,
) -> httpx.Response:
    kwargs: dict[str, Any] = {}
    if json_body is not None:
        kwargs["json"] = json_body
    elif text is not None:
        kwargs["text"] = text
    return httpx.Response(status, headers=headers, **kwargs)


def make_handler(outcomes: list[httpx.Response | Exception]):
    """A sync handler (valid for both AsyncClient and Client MockTransports) that
    returns/raises the next scripted outcome per call, and records every call.
    Raises AssertionError if called more times than scripted (catches
    over-retrying bugs instead of silently indexing past the list).
    """
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        idx = len(calls) - 1
        if idx >= len(outcomes):
            raise AssertionError(
                f"handler called {len(calls)} times; only {len(outcomes)} outcomes scripted"
            )
        outcome = outcomes[idx]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    return handler, calls


def async_transport(
    handler,
    *,
    max_concurrency: int = 8,
    retry: RetryConfig | None = None,
) -> AsyncTransport:
    t = AsyncTransport(
        base_url=BASE_URL,
        headers={},
        limits=_limits(),
        timeout=_timeout(),
        max_concurrency=max_concurrency,
        retry=retry or FAST_RETRY,
    )
    t._client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url=BASE_URL)
    return t


def sync_transport(
    handler,
    *,
    max_concurrency: int = 8,
    retry: RetryConfig | None = None,
) -> SyncTransport:
    t = SyncTransport(
        base_url=BASE_URL,
        headers={},
        limits=_limits(),
        timeout=_timeout(),
        max_concurrency=max_concurrency,
        retry=retry or FAST_RETRY,
    )
    t._client = httpx.Client(transport=httpx.MockTransport(handler), base_url=BASE_URL)
    return t


# --------------------------------------------------------------------------
# 1. Happy path
# --------------------------------------------------------------------------

async def test_async_happy_path_returns_parsed_json() -> None:
    handler, calls = make_handler([resp(200, json_body={"ok": True, "n": 1})])
    t = async_transport(handler)
    result = await t.request(spec())
    assert result == {"ok": True, "n": 1}
    assert len(calls) == 1
    await t.aclose()


def test_sync_happy_path_returns_parsed_json() -> None:
    handler, calls = make_handler([resp(200, json_body={"ok": True, "n": 1})])
    t = sync_transport(handler)
    result = t.request(spec())
    assert result == {"ok": True, "n": 1}
    assert len(calls) == 1
    t.close()


async def test_async_non_json_200_returns_text() -> None:
    handler, _ = make_handler([resp(200, text="plain text, not json{")])
    t = async_transport(handler)
    result = await t.request(spec())
    assert result == "plain text, not json{"
    await t.aclose()


def test_sync_non_json_200_returns_text() -> None:
    handler, _ = make_handler([resp(200, text="plain text, not json{")])
    t = sync_transport(handler)
    result = t.request(spec())
    assert result == "plain text, not json{"
    t.close()


async def test_async_empty_body_200_returns_none() -> None:
    handler, _ = make_handler([resp(200)])
    t = async_transport(handler)
    result = await t.request(spec())
    assert result is None
    await t.aclose()


def test_sync_empty_body_200_returns_none() -> None:
    handler, _ = make_handler([resp(200)])
    t = sync_transport(handler)
    result = t.request(spec())
    assert result is None
    t.close()


# --------------------------------------------------------------------------
# 2. 429 transient (recovers) and 429 exhausted
# --------------------------------------------------------------------------

async def test_async_429_twice_then_success_is_transparent() -> None:
    handler, calls = make_handler(
        [
            resp(429, headers={"Retry-After": "0"}, json_body={"error": "slow down"}),
            resp(429, headers={"Retry-After": "0"}, json_body={"error": "slow down"}),
            resp(200, json_body={"ok": True}),
        ]
    )
    t = async_transport(handler)
    result = await t.request(spec())
    assert result == {"ok": True}
    assert len(calls) == 3
    await t.aclose()


def test_sync_429_twice_then_success_is_transparent() -> None:
    handler, calls = make_handler(
        [
            resp(429, headers={"Retry-After": "0"}, json_body={"error": "slow down"}),
            resp(429, headers={"Retry-After": "0"}, json_body={"error": "slow down"}),
            resp(200, json_body={"ok": True}),
        ]
    )
    t = sync_transport(handler)
    result = t.request(spec())
    assert result == {"ok": True}
    assert len(calls) == 3
    t.close()


async def test_async_429_exhausts_retries_raises_ratelimiterror() -> None:
    retry = RetryConfig(max_retries=3, base=0.001, cap=0.01)
    outcomes = [
        resp(429, headers={"Retry-After": "0"}, json_body={"error": "rate limited", "hint": "slow down"})
        for _ in range(retry.max_retries + 1)
    ]
    handler, calls = make_handler(outcomes)
    t = async_transport(handler, retry=retry)
    with pytest.raises(RateLimitError) as ei:
        await t.request(spec())
    assert len(calls) == retry.max_retries + 1
    assert ei.value.retry_after == 0.0
    assert ei.value.hint == "slow down"
    assert ei.value.status == 429
    await t.aclose()


def test_sync_429_exhausts_retries_raises_ratelimiterror() -> None:
    retry = RetryConfig(max_retries=3, base=0.001, cap=0.01)
    outcomes = [
        resp(429, headers={"Retry-After": "0"}, json_body={"error": "rate limited", "hint": "slow down"})
        for _ in range(retry.max_retries + 1)
    ]
    handler, calls = make_handler(outcomes)
    t = sync_transport(handler, retry=retry)
    with pytest.raises(RateLimitError) as ei:
        t.request(spec())
    assert len(calls) == retry.max_retries + 1
    assert ei.value.retry_after == 0.0
    assert ei.value.hint == "slow down"
    assert ei.value.status == 429
    t.close()


# --------------------------------------------------------------------------
# 3. Retry-After honored (sleep duration) + parse_retry_after unit tests
# --------------------------------------------------------------------------

async def test_async_retry_after_seconds_honored(monkeypatch: pytest.MonkeyPatch) -> None:
    recorded: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        recorded.append(seconds)

    monkeypatch.setattr(transport_mod.asyncio, "sleep", fake_sleep)

    handler, calls = make_handler(
        [resp(429, headers={"Retry-After": "1"}), resp(200, json_body={"ok": True})]
    )
    t = async_transport(handler, retry=RetryConfig(max_retries=3, base=0.001, cap=0.01))
    result = await t.request(spec())
    assert result == {"ok": True}
    assert len(calls) == 2
    assert recorded == [pytest.approx(1.0)]
    await t.aclose()


def test_sync_retry_after_seconds_honored(monkeypatch: pytest.MonkeyPatch) -> None:
    recorded: list[float] = []

    def fake_sleep(seconds: float) -> None:
        recorded.append(seconds)

    monkeypatch.setattr(transport_mod.time, "sleep", fake_sleep)

    handler, calls = make_handler(
        [resp(429, headers={"Retry-After": "1"}), resp(200, json_body={"ok": True})]
    )
    t = sync_transport(handler, retry=RetryConfig(max_retries=3, base=0.001, cap=0.01))
    result = t.request(spec())
    assert result == {"ok": True}
    assert len(calls) == 2
    assert recorded == [pytest.approx(1.0)]
    t.close()


async def test_async_retry_after_capped_by_max_retry_after(monkeypatch: pytest.MonkeyPatch) -> None:
    recorded: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        recorded.append(seconds)

    monkeypatch.setattr(transport_mod.asyncio, "sleep", fake_sleep)

    retry = RetryConfig(max_retries=3, base=0.001, cap=0.01, max_retry_after=0.5)
    outcomes = [
        resp(429, headers={"Retry-After": "5"}) for _ in range(retry.max_retries + 1)
    ]
    handler, calls = make_handler(outcomes)
    t = async_transport(handler, retry=retry)
    with pytest.raises(RateLimitError):
        await t.request(spec())
    assert len(calls) == retry.max_retries + 1
    # One sleep per retry (not on the final, non-retried failure).
    assert recorded == [pytest.approx(0.5)] * retry.max_retries
    await t.aclose()


def test_sync_retry_after_capped_by_max_retry_after(monkeypatch: pytest.MonkeyPatch) -> None:
    recorded: list[float] = []

    def fake_sleep(seconds: float) -> None:
        recorded.append(seconds)

    monkeypatch.setattr(transport_mod.time, "sleep", fake_sleep)

    retry = RetryConfig(max_retries=3, base=0.001, cap=0.01, max_retry_after=0.5)
    outcomes = [
        resp(429, headers={"Retry-After": "5"}) for _ in range(retry.max_retries + 1)
    ]
    handler, calls = make_handler(outcomes)
    t = sync_transport(handler, retry=retry)
    with pytest.raises(RateLimitError):
        t.request(spec())
    assert len(calls) == retry.max_retries + 1
    assert recorded == [pytest.approx(0.5)] * retry.max_retries
    t.close()


def test_parse_retry_after_delta_seconds() -> None:
    assert parse_retry_after("5") == 5.0
    assert parse_retry_after("0") == 0.0
    assert parse_retry_after("2.5") == 2.5


def test_parse_retry_after_none_or_empty_returns_none() -> None:
    assert parse_retry_after(None) is None
    assert parse_retry_after("") is None


def test_parse_retry_after_negative_delta_clamped_to_zero() -> None:
    assert parse_retry_after("-5") == 0.0


def test_parse_retry_after_http_date() -> None:
    future = datetime.now(timezone.utc) + timedelta(seconds=10)
    header_val = email.utils.format_datetime(future, usegmt=True)
    result = parse_retry_after(header_val)
    assert result is not None
    # Generous slack for test execution jitter.
    assert 5.0 <= result <= 15.0


def test_parse_retry_after_past_http_date_clamped_to_zero() -> None:
    past = datetime.now(timezone.utc) - timedelta(seconds=30)
    header_val = email.utils.format_datetime(past, usegmt=True)
    assert parse_retry_after(header_val) == 0.0


@pytest.mark.xfail(
    strict=True,
    reason=(
        "BUG in parse_retry_after (_transport.py): a header value that is neither a "
        "valid float nor a valid HTTP-date raises an uncaught ValueError instead of "
        "returning None. email.utils.parsedate_to_datetime raises ValueError for "
        "unparseable input on Python 3.10+, and the surrounding try/except only "
        "wraps the float() parse, not the parsedate_to_datetime() call. A malformed "
        "Retry-After header from the API would currently crash the transport instead "
        "of gracefully falling back to the computed backoff."
    ),
)
def test_parse_retry_after_garbage_string_should_return_none() -> None:
    assert parse_retry_after("not-a-valid-retry-after-value") is None


# --------------------------------------------------------------------------
# 4. 5xx: retried-to-exhaustion vs. not-in-retry-set
# --------------------------------------------------------------------------

async def test_async_503_retried_to_exhaustion_raises_servererror() -> None:
    retry = RetryConfig(max_retries=3, base=0.001, cap=0.01)
    outcomes = [resp(503, text="unavailable") for _ in range(retry.max_retries + 1)]
    handler, calls = make_handler(outcomes)
    t = async_transport(handler, retry=retry)
    with pytest.raises(ServerError) as ei:
        await t.request(spec())
    assert len(calls) == retry.max_retries + 1
    assert ei.value.status == 503
    await t.aclose()


def test_sync_503_retried_to_exhaustion_raises_servererror() -> None:
    retry = RetryConfig(max_retries=3, base=0.001, cap=0.01)
    outcomes = [resp(503, text="unavailable") for _ in range(retry.max_retries + 1)]
    handler, calls = make_handler(outcomes)
    t = sync_transport(handler, retry=retry)
    with pytest.raises(ServerError) as ei:
        t.request(spec())
    assert len(calls) == retry.max_retries + 1
    assert ei.value.status == 503
    t.close()


async def test_async_500_not_in_retry_set_raises_immediately() -> None:
    handler, calls = make_handler([resp(500, text="boom")])
    t = async_transport(handler)
    with pytest.raises(ServerError) as ei:
        await t.request(spec())
    assert len(calls) == 1
    assert ei.value.status == 500
    await t.aclose()


def test_sync_500_not_in_retry_set_raises_immediately() -> None:
    handler, calls = make_handler([resp(500, text="boom")])
    t = sync_transport(handler)
    with pytest.raises(ServerError) as ei:
        t.request(spec())
    assert len(calls) == 1
    assert ei.value.status == 500
    t.close()


@pytest.mark.parametrize("status", [429, 502, 503, 504])
def test_should_retry_true_for_retryable_statuses(status: int) -> None:
    assert _should_retry(resp(status), None) is True


@pytest.mark.parametrize("status", [400, 401, 403, 404, 500, 501])
def test_should_retry_false_for_non_retryable_statuses(status: int) -> None:
    assert _should_retry(resp(status), None) is False


# --------------------------------------------------------------------------
# 5. 4xx: no retry, correct exception mapping
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "status,exc_type",
    [(404, NotFoundError), (401, AuthError), (403, AuthError), (400, APIError)],
)
async def test_async_4xx_maps_to_expected_error_no_retry(status: int, exc_type: type) -> None:
    handler, calls = make_handler([resp(status, json_body={"error": "bad"})])
    t = async_transport(handler)
    with pytest.raises(exc_type) as ei:
        await t.request(spec())
    assert len(calls) == 1
    assert ei.value.status == status
    if exc_type is APIError:
        # Must be the plain base, not accidentally a subclass instance.
        assert type(ei.value) is APIError
    await t.aclose()


@pytest.mark.parametrize(
    "status,exc_type",
    [(404, NotFoundError), (401, AuthError), (403, AuthError), (400, APIError)],
)
def test_sync_4xx_maps_to_expected_error_no_retry(status: int, exc_type: type) -> None:
    handler, calls = make_handler([resp(status, json_body={"error": "bad"})])
    t = sync_transport(handler)
    with pytest.raises(exc_type) as ei:
        t.request(spec())
    assert len(calls) == 1
    assert ei.value.status == status
    if exc_type is APIError:
        assert type(ei.value) is APIError
    t.close()


# --------------------------------------------------------------------------
# 6. Network exceptions
# --------------------------------------------------------------------------

async def test_async_connect_error_retried_then_connectionfailed() -> None:
    retry = RetryConfig(max_retries=3, base=0.001, cap=0.01)
    outcomes: list[httpx.Response | Exception] = [
        httpx.ConnectError("connection refused") for _ in range(retry.max_retries + 1)
    ]
    handler, calls = make_handler(outcomes)
    t = async_transport(handler, retry=retry)
    with pytest.raises(ConnectionFailed):
        await t.request(spec())
    assert len(calls) == retry.max_retries + 1
    await t.aclose()


def test_sync_connect_error_retried_then_connectionfailed() -> None:
    retry = RetryConfig(max_retries=3, base=0.001, cap=0.01)
    outcomes: list[httpx.Response | Exception] = [
        httpx.ConnectError("connection refused") for _ in range(retry.max_retries + 1)
    ]
    handler, calls = make_handler(outcomes)
    t = sync_transport(handler, retry=retry)
    with pytest.raises(ConnectionFailed):
        t.request(spec())
    assert len(calls) == retry.max_retries + 1
    t.close()


async def test_async_connect_error_then_recovers_transparently() -> None:
    handler, calls = make_handler([httpx.ConnectError("boom"), resp(200, json_body={"ok": True})])
    t = async_transport(handler)
    result = await t.request(spec())
    assert result == {"ok": True}
    assert len(calls) == 2
    await t.aclose()


async def test_async_read_timeout_retried_then_pytheumtimeout() -> None:
    retry = RetryConfig(max_retries=3, base=0.001, cap=0.01)
    outcomes: list[httpx.Response | Exception] = [
        httpx.ReadTimeout("timed out") for _ in range(retry.max_retries + 1)
    ]
    handler, calls = make_handler(outcomes)
    t = async_transport(handler, retry=retry)
    with pytest.raises(PytheumTimeout):
        await t.request(spec())
    assert len(calls) == retry.max_retries + 1
    await t.aclose()


def test_sync_read_timeout_retried_then_pytheumtimeout() -> None:
    retry = RetryConfig(max_retries=3, base=0.001, cap=0.01)
    outcomes: list[httpx.Response | Exception] = [
        httpx.ReadTimeout("timed out") for _ in range(retry.max_retries + 1)
    ]
    handler, calls = make_handler(outcomes)
    t = sync_transport(handler, retry=retry)
    with pytest.raises(PytheumTimeout):
        t.request(spec())
    assert len(calls) == retry.max_retries + 1
    t.close()


# --------------------------------------------------------------------------
# 7. RequestSpec.clean_params
# --------------------------------------------------------------------------

def test_clean_params_drops_none_and_coerces_bools() -> None:
    s = RequestSpec(
        method="GET",
        path="/x",
        params={"a": None, "b": True, "c": False, "d": "keep", "e": 0, "f": 3.5},
    )
    assert s.clean_params() == {"b": "true", "c": "false", "d": "keep", "e": 0, "f": 3.5}


def test_clean_params_empty_dict_stays_empty() -> None:
    assert RequestSpec(method="GET", path="/x", params={}).clean_params() == {}


def test_clean_params_all_none_drops_everything() -> None:
    s = RequestSpec(method="GET", path="/x", params={"a": None, "b": None})
    assert s.clean_params() == {}


# --------------------------------------------------------------------------
# 8. RetryConfig.backoff bounds
# --------------------------------------------------------------------------

def test_retryconfig_backoff_within_bounds_over_many_samples() -> None:
    rc = RetryConfig(base=0.1, cap=1.0)
    for attempt in range(8):
        ceil = min(rc.cap, rc.base * (2**attempt))
        samples = [rc.backoff(attempt) for _ in range(300)]
        assert all(0.0 <= s <= ceil for s in samples)
        # The random.uniform range should actually get exercised at both ends
        # across enough samples (sanity check it's not a constant).
        if ceil > 0:
            assert max(samples) > 0.0


def test_retryconfig_backoff_ceiling_is_monotonic_non_decreasing_then_capped() -> None:
    rc = RetryConfig(base=0.1, cap=1.0)
    ceils = [min(rc.cap, rc.base * (2**attempt)) for attempt in range(10)]
    assert ceils == sorted(ceils)
    # Confirm the cap actually engages (uncapped growth would exceed cap by attempt 4+).
    assert ceils[-1] == rc.cap
    assert rc.base * (2**9) > rc.cap


def test_retryconfig_backoff_never_exceeds_cap_even_at_high_attempt() -> None:
    rc = RetryConfig(base=0.25, cap=8.0)
    samples = [rc.backoff(50) for _ in range(200)]
    assert all(0.0 <= s <= rc.cap for s in samples)


# --------------------------------------------------------------------------
# 9. Governor concurrency (load-bearing)
# --------------------------------------------------------------------------

async def test_async_governor_never_exceeds_max_concurrency() -> None:
    max_concurrency = 2
    n_requests = 10
    state = {"current": 0, "peak": 0, "completed": 0}

    async def handler(request: httpx.Request) -> httpx.Response:
        state["current"] += 1
        state["peak"] = max(state["peak"], state["current"])
        # Yield control so other in-flight coroutines get a chance to run
        # concurrently within the governor's window.
        await asyncio.sleep(0.03)
        state["current"] -= 1
        state["completed"] += 1
        return httpx.Response(200, json={"ok": True})

    t = async_transport(handler, max_concurrency=max_concurrency)
    results = await asyncio.gather(*[t.request(spec()) for _ in range(n_requests)])

    assert results == [{"ok": True}] * n_requests
    assert state["completed"] == n_requests
    # Airtight: peak in-flight must never exceed the governor's bound...
    assert state["peak"] <= max_concurrency
    # ...and with 10 requests racing for 2 slots, the bound must actually be
    # reached (otherwise the governor isn't being exercised at all).
    assert state["peak"] == max_concurrency
    assert state["current"] == 0
    await t.aclose()


async def test_async_governor_of_one_fully_serializes_requests() -> None:
    state = {"current": 0, "peak": 0}

    async def handler(request: httpx.Request) -> httpx.Response:
        state["current"] += 1
        state["peak"] = max(state["peak"], state["current"])
        await asyncio.sleep(0.01)
        state["current"] -= 1
        return httpx.Response(200, json={"ok": True})

    t = async_transport(handler, max_concurrency=1)
    await asyncio.gather(*[t.request(spec()) for _ in range(5)])
    assert state["peak"] == 1
    await t.aclose()


# --------------------------------------------------------------------------
# 10. Clean shutdown
# --------------------------------------------------------------------------

async def test_async_transport_aclose_closes_underlying_client() -> None:
    handler, _ = make_handler([resp(200, json_body={"ok": True})])
    t = async_transport(handler)
    assert t._client.is_closed is False
    await t.aclose()
    assert t._client.is_closed is True


def test_sync_transport_close_closes_underlying_client() -> None:
    handler, _ = make_handler([resp(200, json_body={"ok": True})])
    t = sync_transport(handler)
    assert t._client.is_closed is False
    t.close()
    assert t._client.is_closed is True


# --------------------------------------------------------------------------
# Misc: build_limits / build_timeout smoke (cheap, cross-checks the factories
# used by every transport construction above).
# --------------------------------------------------------------------------

def test_build_limits_sets_fields() -> None:
    limits = build_limits(max_connections=10, max_keepalive=5)
    assert limits.max_connections == 10
    assert limits.max_keepalive_connections == 5


def test_build_timeout_sets_fields() -> None:
    timeout = build_timeout(connect=1.0, read=2.0, write=3.0, pool=4.0)
    assert timeout.connect == 1.0
    assert timeout.read == 2.0
    assert timeout.write == 3.0
    assert timeout.pool == 4.0
