"""Guards that the client surface stays in lock-step with the endpoint registry.

If a new REST route is added to _registry.py but not wrapped as a client method
(or vice versa), or a param is declared but not forwardable, these fail loudly.
"""
from __future__ import annotations

from pytheum.client import AsyncClient, Client
from pytheum.client._registry import BY_NAME, REGISTRY


def test_every_registry_endpoint_has_async_and_sync_methods() -> None:
    for ep in REGISTRY:
        assert callable(getattr(AsyncClient, ep.name, None)), f"AsyncClient missing {ep.name}()"
        assert callable(getattr(Client, ep.name, None)), f"Client missing {ep.name}()"


def test_no_orphan_methods_without_convenience() -> None:
    # find_divergences is an intentional convenience (no own route); everything else
    # public + non-dunder should map to a registry endpoint or a known helper.
    helpers = {"gather", "aclose", "close", "get_markets", "equivalents_many",
               "find_divergences"}
    for cls in (AsyncClient, Client):
        for name in dir(cls):
            if name.startswith("_") or name in helpers:
                continue
            attr = getattr(cls, name)
            if callable(attr) and name.islower():
                assert name in BY_NAME, f"{cls.__name__}.{name}() has no registry endpoint"


def test_registry_paths_and_params_wellformed() -> None:
    seen = set()
    for ep in REGISTRY:
        assert ep.name not in seen, f"duplicate endpoint {ep.name}"
        seen.add(ep.name)
        assert ep.method in {"GET", "POST"}
        assert ep.path.startswith("/v1/")
        assert isinstance(ep.params, tuple)


def test_spec_builder_forwards_only_declared_params() -> None:
    # bogus params are dropped; declared ones pass; ref is url-encoded
    spec = Client._spec("equivalents", ref="polymarket:2702984", limit=5, bogus="x")
    assert "%3A" in spec.path and spec.params == {"limit": 5}
