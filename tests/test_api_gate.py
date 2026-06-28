"""Tests for the HTTP API edge gate: API-key auth + per-client rate limiting.

Covers:
  - enforcement OFF (default) → unchanged behaviour, no wrapping
  - enforcement ON → valid key passes, invalid/missing key 401
  - keyless allowlist (/v1/status, /v1/metrics, /healthz) bypasses auth
  - OPTIONS preflight bypasses auth
  - rate-limit 429 + Retry-After, and reset after refill
"""
from __future__ import annotations

from typing import Any

import httpx
import pytest

from pytheum.api.gate import ApiGate, maybe_wrap
from pytheum.config import ServeConfig
from pytheum.routing import Router, RouterApp


def _router() -> Router:
    router = Router()

    async def status(query: dict[str, str]) -> Any:
        return 200, {"ok": "status"}

    async def metrics(query: dict[str, str]) -> Any:
        return 200, {"ok": "metrics"}

    async def matched(query: dict[str, str]) -> Any:
        return 200, {"ok": "matched"}

    router.add("GET", "/v1/status", status)
    router.add("GET", "/v1/metrics", metrics)
    router.add("GET", "/v1/markets/matched", matched)
    return router


def _client(app: Any) -> httpx.AsyncClient:
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://test")


# --------------------------------------------------------------------------- #
# maybe_wrap — OFF by default
# --------------------------------------------------------------------------- #


def test_maybe_wrap_passthrough_when_explicitly_disabled() -> None:
    inner = RouterApp(_router())
    # Explicitly disabled (e.g. a single-user self-host): zero-cost pass-through.
    cfg = ServeConfig(require_api_key=False, rate_limit_per_min=0)
    wrapped = maybe_wrap(inner, cfg)
    assert wrapped is inner  # same object, no gate


def test_default_config_is_rate_limited_by_default(monkeypatch) -> None:
    # Hardened default: the gate is ON (per-IP rate limiting) out of the box so the
    # public/hosted instance is throttled against request floods; auth stays off
    # (keyless, open-but-rate-limited). Env overrides are cleared to test the field.
    monkeypatch.delenv("PYTHEUM_RATE_LIMIT_PER_MIN", raising=False)
    monkeypatch.delenv("PYTHEUM_REQUIRE_API_KEY", raising=False)
    cfg = ServeConfig()
    assert cfg.rate_limit_per_min == 120
    assert cfg.require_api_key is False
    assert isinstance(maybe_wrap(RouterApp(_router()), cfg), ApiGate)


def test_maybe_wrap_wraps_when_auth_on() -> None:
    inner = RouterApp(_router())
    cfg = ServeConfig(require_api_key=True, api_keys="k1")
    wrapped = maybe_wrap(inner, cfg)
    assert isinstance(wrapped, ApiGate)


def test_maybe_wrap_wraps_when_rate_limit_on() -> None:
    inner = RouterApp(_router())
    cfg = ServeConfig(require_api_key=False, rate_limit_per_min=60)
    wrapped = maybe_wrap(inner, cfg)
    assert isinstance(wrapped, ApiGate)


# --------------------------------------------------------------------------- #
# Auth enforcement
# --------------------------------------------------------------------------- #


def _auth_gate() -> ApiGate:
    return ApiGate(
        RouterApp(_router()),
        require_api_key=True,
        api_keys=frozenset({"good-key", "second-key"}),
        rate_per_min=0.0,  # rate limiting off — isolate auth
        burst=0.0,
    )


@pytest.mark.asyncio
async def test_auth_off_allows_unauthenticated() -> None:
    gate = ApiGate(
        RouterApp(_router()),
        require_api_key=False,
        api_keys=frozenset(),
        rate_per_min=0.0,
        burst=0.0,
    )
    async with _client(gate) as c:
        resp = await c.get("/v1/markets/matched")
    assert resp.status_code == 200
    assert resp.json() == {"ok": "matched"}


@pytest.mark.asyncio
async def test_auth_on_rejects_missing_key() -> None:
    async with _client(_auth_gate()) as c:
        resp = await c.get("/v1/markets/matched")
    assert resp.status_code == 401
    assert resp.json()["error"] == "unauthorized"


@pytest.mark.asyncio
async def test_auth_on_rejects_invalid_key() -> None:
    async with _client(_auth_gate()) as c:
        resp = await c.get("/v1/markets/matched", headers={"X-API-Key": "nope"})
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_auth_on_accepts_valid_x_api_key() -> None:
    async with _client(_auth_gate()) as c:
        resp = await c.get("/v1/markets/matched", headers={"X-API-Key": "good-key"})
    assert resp.status_code == 200
    assert resp.json() == {"ok": "matched"}


@pytest.mark.asyncio
async def test_auth_on_accepts_valid_bearer_token() -> None:
    async with _client(_auth_gate()) as c:
        resp = await c.get(
            "/v1/markets/matched",
            headers={"Authorization": "Bearer second-key"},
        )
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_auth_on_keyless_status_bypasses() -> None:
    async with _client(_auth_gate()) as c:
        resp = await c.get("/v1/status")
    assert resp.status_code == 200
    assert resp.json() == {"ok": "status"}


@pytest.mark.asyncio
async def test_auth_on_keyless_metrics_bypasses() -> None:
    async with _client(_auth_gate()) as c:
        resp = await c.get("/v1/metrics")
    assert resp.status_code == 200
    assert resp.json() == {"ok": "metrics"}


@pytest.mark.asyncio
async def test_auth_on_options_preflight_bypasses() -> None:
    async with _client(_auth_gate()) as c:
        resp = await c.options("/v1/markets/matched")
    # No OPTIONS route is registered, so it reaches RouterApp and 404s —
    # the point is it is NOT 401 (auth was bypassed for the preflight).
    assert resp.status_code != 401


@pytest.mark.asyncio
async def test_auth_required_but_no_keys_configured_fails_closed() -> None:
    gate = ApiGate(
        RouterApp(_router()),
        require_api_key=True,
        api_keys=frozenset(),  # misconfiguration: enforce but no keys
        rate_per_min=0.0,
        burst=0.0,
    )
    async with _client(gate) as c:
        resp = await c.get("/v1/markets/matched", headers={"X-API-Key": "anything"})
    assert resp.status_code == 401  # fail-closed


# --------------------------------------------------------------------------- #
# Rate limiting
# --------------------------------------------------------------------------- #


def _rl_gate(per_min: float, burst: float) -> ApiGate:
    return ApiGate(
        RouterApp(_router()),
        require_api_key=False,
        api_keys=frozenset(),
        rate_per_min=per_min,
        burst=burst,
    )


@pytest.mark.asyncio
async def test_rate_limit_429_after_burst() -> None:
    # burst of 3, refill effectively 0 within the test window.
    gate = _rl_gate(per_min=0.001, burst=3.0)
    async with _client(gate) as c:
        codes = [
            (await c.get("/v1/markets/matched")).status_code for _ in range(5)
        ]
    # First 3 allowed, then 429s.
    assert codes[:3] == [200, 200, 200]
    assert codes[3] == 429
    assert codes[4] == 429


@pytest.mark.asyncio
async def test_rate_limit_429_carries_retry_after() -> None:
    gate = _rl_gate(per_min=0.001, burst=1.0)
    async with _client(gate) as c:
        await c.get("/v1/markets/matched")  # consume the single token
        resp = await c.get("/v1/markets/matched")
    assert resp.status_code == 429
    assert resp.json()["error"] == "rate_limited"
    assert "retry-after" in {k.lower() for k in resp.headers}


@pytest.mark.asyncio
async def test_rate_limit_resets_after_refill() -> None:
    # High refill so the bucket refills within the test: 6000/min = 100/s.
    gate = _rl_gate(per_min=6000.0, burst=1.0)
    async with _client(gate) as c:
        first = (await c.get("/v1/markets/matched")).status_code
        blocked = (await c.get("/v1/markets/matched")).status_code
        # Manually advance the bucket by mutating last so refill is deterministic
        # (avoids a real sleep in the test).
        import time
        for b in gate._buckets.values():
            b.last = time.monotonic() - 1.0  # 1s elapsed -> +100 tokens
        after = (await c.get("/v1/markets/matched")).status_code
    assert first == 200
    assert blocked == 429
    assert after == 200


@pytest.mark.asyncio
async def test_rate_limit_keyless_paths_not_limited() -> None:
    gate = _rl_gate(per_min=0.001, burst=1.0)
    async with _client(gate) as c:
        # Hammer /v1/status well past the burst — keyless paths never limit.
        codes = [(await c.get("/v1/status")).status_code for _ in range(5)]
    assert codes == [200] * 5


@pytest.mark.asyncio
async def test_rate_limit_per_client_isolation() -> None:
    # Auth + rate limit together: each key gets its own bucket.
    gate = ApiGate(
        RouterApp(_router()),
        require_api_key=True,
        api_keys=frozenset({"a", "b"}),
        rate_per_min=0.001,
        burst=1.0,
    )
    async with _client(gate) as c:
        a1 = (await c.get("/v1/markets/matched", headers={"X-API-Key": "a"})).status_code
        a2 = (await c.get("/v1/markets/matched", headers={"X-API-Key": "a"})).status_code
        b1 = (await c.get("/v1/markets/matched", headers={"X-API-Key": "b"})).status_code
    assert a1 == 200
    assert a2 == 429  # key "a" exhausted its bucket
    assert b1 == 200  # key "b" has its own independent bucket
