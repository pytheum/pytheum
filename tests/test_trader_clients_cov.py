"""Coverage tests for pytheum.trader.clients.TraderClients.

pytheum-core is an optional runtime dependency NOT installed in this public
repo, so TraderClients.start() (which imports KalshiClient / PolymarketClient
lazily) can never run against the real package here. We inject lightweight fake
``pytheum_core.venues.*`` modules into sys.modules so the deferred imports
resolve to stubs — exercising construction, idempotency, the .ready property,
and the stop()/aclose() lifecycle without any network or real client.

NEVER opens a socket — both fake clients are pure in-memory stubs.
"""
from __future__ import annotations

import sys
import types
from typing import Any

import pytest

from pytheum.trader.clients import TraderClients, _get_clients

# ─────────────────────────────────────────────────────────────────────────────
# Fake pytheum_core.venues.{kalshi,polymarket}.client modules
# ─────────────────────────────────────────────────────────────────────────────


class _FakeVenueClient:
    """Stand-in for KalshiClient / PolymarketClient (no signer, no network)."""

    instances: list[_FakeVenueClient] = []

    def __init__(self) -> None:
        self.closed = False
        _FakeVenueClient.instances.append(self)

    async def aclose(self) -> None:
        self.closed = True


@pytest.fixture
def _fake_core(monkeypatch: pytest.MonkeyPatch) -> type[_FakeVenueClient]:
    """Install fake pytheum_core.venues.* modules so deferred imports resolve."""
    _FakeVenueClient.instances = []

    def _mk_module(name: str, **attrs: Any) -> types.ModuleType:
        mod = types.ModuleType(name)
        for k, v in attrs.items():
            setattr(mod, k, v)
        return mod

    # Build the package tree pytheum_core -> venues -> {kalshi,polymarket} -> client
    pkg_core = _mk_module("pytheum_core")
    pkg_venues = _mk_module("pytheum_core.venues")
    pkg_kalshi = _mk_module("pytheum_core.venues.kalshi")
    pkg_pm = _mk_module("pytheum_core.venues.polymarket")
    mod_kalshi_client = _mk_module(
        "pytheum_core.venues.kalshi.client", KalshiClient=_FakeVenueClient
    )
    mod_pm_client = _mk_module(
        "pytheum_core.venues.polymarket.client", PolymarketClient=_FakeVenueClient
    )

    for name, mod in [
        ("pytheum_core", pkg_core),
        ("pytheum_core.venues", pkg_venues),
        ("pytheum_core.venues.kalshi", pkg_kalshi),
        ("pytheum_core.venues.polymarket", pkg_pm),
        ("pytheum_core.venues.kalshi.client", mod_kalshi_client),
        ("pytheum_core.venues.polymarket.client", mod_pm_client),
    ]:
        monkeypatch.setitem(sys.modules, name, mod)

    return _FakeVenueClient


# ─────────────────────────────────────────────────────────────────────────────
# Construction / lifecycle
# ─────────────────────────────────────────────────────────────────────────────


def test_init_state_before_start() -> None:
    clients = TraderClients()
    assert clients.kalshi is None
    assert clients.polymarket is None
    assert clients.ready is False


async def test_start_constructs_both_clients(_fake_core: type[_FakeVenueClient]) -> None:
    clients = TraderClients()
    await clients.start()
    assert clients.kalshi is not None
    assert clients.polymarket is not None
    assert clients.ready is True
    # exactly two clients constructed
    assert len(_fake_core.instances) == 2


async def test_start_is_idempotent(_fake_core: type[_FakeVenueClient]) -> None:
    clients = TraderClients()
    await clients.start()
    first_kalshi = clients.kalshi
    first_pm = clients.polymarket
    await clients.start()  # second call must not reconstruct
    assert clients.kalshi is first_kalshi
    assert clients.polymarket is first_pm
    assert len(_fake_core.instances) == 2  # not 4


async def test_stop_closes_and_clears(_fake_core: type[_FakeVenueClient]) -> None:
    clients = TraderClients()
    await clients.start()
    kalshi_obj = clients.kalshi
    pm_obj = clients.polymarket
    await clients.stop()
    assert clients.kalshi is None
    assert clients.polymarket is None
    assert clients.ready is False
    assert kalshi_obj.closed is True  # type: ignore[union-attr]
    assert pm_obj.closed is True  # type: ignore[union-attr]


async def test_stop_when_never_started_is_noop() -> None:
    clients = TraderClients()
    # No clients constructed — stop() must not raise on the None branches.
    await clients.stop()
    assert clients.kalshi is None
    assert clients.polymarket is None


async def test_start_stop_start_roundtrip(_fake_core: type[_FakeVenueClient]) -> None:
    clients = TraderClients()
    await clients.start()
    await clients.stop()
    await clients.start()  # reconstructs after a stop
    assert clients.ready is True
    # 2 from first start + 2 from second start
    assert len(_fake_core.instances) == 4


async def test_ready_false_when_only_one_present(_fake_core: type[_FakeVenueClient]) -> None:
    clients = TraderClients()
    await clients.start()
    clients.polymarket = None  # simulate a half-torn-down state
    assert clients.ready is False


# ─────────────────────────────────────────────────────────────────────────────
# _get_clients helper
# ─────────────────────────────────────────────────────────────────────────────


def test_get_clients_from_trader_clients() -> None:
    clients = TraderClients()
    k, p = _get_clients(clients)
    assert k is None and p is None


def test_get_clients_from_duck_typed_stub() -> None:
    class _Stub:
        kalshi = "K"
        polymarket = "P"

    k, p = _get_clients(_Stub())
    assert k == "K"
    assert p == "P"


def test_get_clients_missing_attrs_returns_none() -> None:
    k, p = _get_clients(object())
    assert k is None
    assert p is None
