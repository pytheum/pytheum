"""RouterRegistry — declarative route registry with replace-on-duplicate semantics.

Design contract (from the 2026-06-13 restructure blueprint §1):

* ``RouteSpec`` captures a route's method, pattern, handler, and OpenAPI metadata
  (summary, tags, per-query-param descriptions).
* ``RouterRegistry.add()`` stores specs keyed by ``(method.upper(), pattern)``.
  **The last registration for a given key wins.**  This is the mechanism by which
  ``pytheum-pit`` overrides serve-side routes: pit calls ``registry.add()`` a
  second time for e.g. ``GET /v1/markets/{ref}/ohlcv`` with its archive-aware
  ``PitArchiveOhlcv`` handler, and that supersedes the venue-fallback handler
  registered by serve.
* ``build_router()`` materialises a :class:`~pytheum.routing.Router` from the
  current spec snapshot.
* ``openapi_paths()`` returns an OpenAPI 3.1-compatible ``paths`` dict suitable
  for embedding in a full spec document.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from pytheum.routing import Handler, Router


@dataclass
class RouteSpec:
    """Specification for a single registered route.

    Attributes:
        method:  HTTP method (case-insensitive; stored upper).
        pattern: URL pattern, e.g. ``/v1/markets/{ref}/ohlcv``.
        handler: Async callable with signature ``(*path_args, query) -> (status, body)``.
        summary: One-line OpenAPI summary string.
        tags:    OpenAPI tag list for grouping in rendered docs.
        params:  Mapping of query-param name → description for OpenAPI schema.
    """

    method: str
    pattern: str
    handler: Handler
    summary: str = ""
    tags: list[str] = field(default_factory=list)
    params: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.method = self.method.upper()


class RouterRegistry:
    """Declarative registry that builds a :class:`~pytheum.routing.Router`.

    Replace-on-duplicate: registering the same ``(method, pattern)`` twice
    silently discards the earlier entry so that the **last** registration wins.
    This is intentional — it lets ``pytheum-pit`` override serve-side routes
    without ceremony.
    """

    def __init__(self) -> None:
        # Ordered dict preserves insertion order for stable openapi output.
        self._specs: dict[tuple[str, str], RouteSpec] = {}

    def add(self, spec: RouteSpec) -> None:
        """Register *spec*, replacing any prior registration for the same key."""
        self._specs[(spec.method, spec.pattern)] = spec

    def build_router(self) -> Router:
        """Materialise a :class:`~pytheum.routing.Router` from the current specs."""
        router = Router()
        for spec in self._specs.values():
            router.add(spec.method, spec.pattern, spec.handler)
        return router

    def openapi_paths(self) -> dict[str, Any]:
        """Return an OpenAPI 3.1-compatible ``paths`` object.

        Path parameters (``{name}`` segments) are emitted as required path
        parameters.  Query params from :attr:`RouteSpec.params` are emitted
        as optional query parameters with their description strings.
        """
        paths: dict[str, Any] = {}
        for spec in self._specs.values():
            openapi_path = spec.pattern
            path_param_names = re.findall(r"\{(\w+)\}", spec.pattern)

            parameters: list[dict[str, Any]] = [
                {
                    "name": p,
                    "in": "path",
                    "required": True,
                    "schema": {"type": "string"},
                }
                for p in path_param_names
            ]
            for qname, qdesc in spec.params.items():
                parameters.append(
                    {
                        "name": qname,
                        "in": "query",
                        "required": False,
                        "description": qdesc,
                        "schema": {"type": "string"},
                    }
                )

            operation: dict[str, Any] = {
                "responses": {"200": {"description": "OK"}},
            }
            if spec.summary:
                operation["summary"] = spec.summary
            if spec.tags:
                operation["tags"] = spec.tags
            if parameters:
                operation["parameters"] = parameters

            if openapi_path not in paths:
                paths[openapi_path] = {}
            paths[openapi_path][spec.method.lower()] = operation

        return paths

    # ------------------------------------------------------------------
    # Introspection helpers
    # ------------------------------------------------------------------

    def routes(self) -> list[RouteSpec]:
        """Return a snapshot of all registered specs in registration order."""
        return list(self._specs.values())

    def __len__(self) -> int:
        return len(self._specs)

    def __repr__(self) -> str:
        return f"RouterRegistry({len(self)} routes)"
