#!/usr/bin/env python3
"""Generate (or check) the committed openapi.yaml from the RouterRegistry.

Usage::

    # Write / overwrite openapi.yaml at repo root:
    python scripts/gen_openapi.py

    # CI check — exit non-zero if the committed file is stale:
    python scripts/gen_openapi.py --check

The script builds a stub RouterRegistry populated with the serve-side routes
from the Stage-0 route inventory (routes classified **S** in the 2026-06-13
reference).  PIT-side routes are omitted here; pytheum-pit will extend the
registry at runtime.

When ``--check`` is passed the freshly generated YAML is compared character-
for-character against ``openapi.yaml`` at the repo root.  Any drift causes a
non-zero exit so CI catches uncommitted spec changes.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

import yaml  # type: ignore[import-untyped]

# Allow running from repo root without installing as a package.
_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT / "src"))

from pytheum.registry import RouterRegistry, RouteSpec  # noqa: E402

_OPENAPI_PATH = _REPO_ROOT / "openapi.yaml"

# ---------------------------------------------------------------------------
# Stub handler — all routes share the same no-op for spec generation only.
# ---------------------------------------------------------------------------


async def _stub(query: dict[str, str]) -> tuple[int, dict[str, Any]]:  # pragma: no cover
    return 200, {}


async def _stub_ref(  # pragma: no cover
    ref: str, query: dict[str, str]
) -> tuple[int, dict[str, Any]]:
    return 200, {}


# ---------------------------------------------------------------------------
# Serve-side route registry (Stage-0 S-boundary routes)
# ---------------------------------------------------------------------------


def build_stub_registry() -> RouterRegistry:
    """Return a RouterRegistry populated with the serve-side route inventory."""
    reg = RouterRegistry()

    reg.add(RouteSpec(
        "GET", "/v1/status", _stub,
        summary="Service health check and dataset summary — keyless.",
        tags=["meta"],
    ))
    reg.add(RouteSpec(
        "GET", "/v1/markets/screen", _stub,
        summary="Structured market screen: filter by venue, status, volume, liquidity.",
        tags=["markets"],
        params={
            "venue": "Filter by venue: kalshi | polymarket",
            "status": "Filter by resolution status: open | resolved",
            "limit": "Maximum number of results (default 50)",
        },
    ))
    reg.add(RouteSpec(
        "GET", "/v1/markets/equivalents", _stub,
        summary="Collection of verified Kalshi<->Polymarket pairs with live quotes.",
        tags=["equivalence"],
        params={
            "limit": "Maximum number of pairs (default 50)",
            "offset": "Pagination offset",
        },
    ))
    reg.add(RouteSpec(
        "GET", "/v1/markets/matched", _stub,
        summary="Paginated view of all 136k+ settlement-verified cross-venue pairs.",
        tags=["equivalence"],
        params={
            "league": "Filter by league / domain",
            "date": "Filter by event date (YYYY-MM-DD)",
            "limit": "Maximum number of results (default 50)",
            "offset": "Pagination offset",
        },
    ))
    reg.add(RouteSpec(
        "GET", "/v1/markets/whale-trades", _stub,
        summary="Recent large-notional Polymarket trades above a USD threshold.",
        tags=["trader"],
        params={"min_usd": "Minimum notional in USD (default 5000)"},
    ))
    reg.add(RouteSpec(
        "GET", "/v1/markets/{ref}/equivalents", _stub_ref,
        summary="Settlement-verified counterpart market on the other venue.",
        tags=["equivalence"],
    ))
    reg.add(RouteSpec(
        "GET", "/v1/markets/{ref}/rules", _stub_ref,
        summary="Full resolution rules for a market and its cross-venue equivalent.",
        tags=["equivalence"],
    ))
    reg.add(RouteSpec(
        "GET", "/v1/markets/{ref}/related", _stub_ref,
        summary="Correlated (non-equivalent) cross-venue markets with basis notes.",
        tags=["related"],
    ))
    reg.add(RouteSpec(
        "GET", "/v1/markets/{ref}/book", _stub_ref,
        summary="Live orderbook snapshot with top-of-book summary.",
        tags=["trader"],
    ))
    reg.add(RouteSpec(
        "GET", "/v1/markets/{ref}/trades", _stub_ref,
        summary="Recent trade tape for a market (live venue fetch, cached ~10 s).",
        tags=["trader"],
    ))
    reg.add(RouteSpec(
        "GET", "/v1/markets/{ref}/oi", _stub_ref,
        summary="Current open interest (live venue fetch, cached ~30 s).",
        tags=["trader"],
    ))
    reg.add(RouteSpec(
        "GET", "/v1/markets/{ref}/ohlcv", _stub_ref,
        summary="OHLCV candles (venue-live source; interval 1m|5m|15m|1h|1d).",
        tags=["trader"],
        params={
            "interval": "Candle interval: 1m|5m|15m|1h|1d (default 1h)",
            "since": "Start of range (ISO-8601 or Unix-seconds)",
            "until": "End of range (ISO-8601 or Unix-seconds)",
            "limit": "Max candles (default 200, max 1000)",
        },
    ))
    reg.add(RouteSpec(
        "GET", "/v1/markets/{ref}/holders", _stub_ref,
        summary="Token holder breakdown for a Polymarket market.",
        tags=["trader"],
    ))
    reg.add(RouteSpec(
        "GET", "/v1/traders/leaderboard", _stub,
        summary="Polymarket trader leaderboard ranked by profit.",
        tags=["trader"],
        params={"period": "Ranking period: weekly | monthly (default weekly)"},
    ))
    reg.add(RouteSpec(
        "GET", "/v1/traders/{wallet}", _stub_ref,
        summary="Polymarket trader profile: positions, recent activity, portfolio value.",
        tags=["trader"],
    ))

    return reg


# ---------------------------------------------------------------------------
# OpenAPI document assembly
# ---------------------------------------------------------------------------


def build_spec(registry: RouterRegistry) -> dict[str, Any]:
    return {
        "openapi": "3.1.0",
        "info": {
            "title": "Pytheum API",
            "version": "0.0.1",
            "description": (
                "The verified prediction-market graph — 136k+ settlement-verified "
                "cross-venue pairs (Kalshi<->Polymarket), live orderbook quotes, "
                "news/social context, and trader analytics."
            ),
            "license": {"name": "MIT", "url": "https://opensource.org/licenses/MIT"},
            "contact": {"url": "https://pytheum.com"},
        },
        "servers": [{"url": "https://api.pytheum.com", "description": "Production"}],
        "paths": registry.openapi_paths(),
    }


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="Diff fresh output against committed openapi.yaml; exit 1 if stale.",
    )
    parser.add_argument(
        "--output",
        metavar="PATH",
        default=str(_OPENAPI_PATH),
        help=f"Destination file (default: {_OPENAPI_PATH})",
    )
    args = parser.parse_args(argv)

    registry = build_stub_registry()
    spec = build_spec(registry)
    fresh = yaml.dump(spec, default_flow_style=False, sort_keys=False, allow_unicode=True)

    if args.check:
        committed_path = Path(args.output)
        if not committed_path.exists():
            print(
                f"error: {committed_path} does not exist — run gen_openapi.py to generate it",
                file=sys.stderr,
            )
            return 1
        committed = committed_path.read_text(encoding="utf-8")
        if fresh == committed:
            print("openapi.yaml is up to date.")
            return 0
        print(
            "error: openapi.yaml is stale — run python scripts/gen_openapi.py to regenerate",
            file=sys.stderr,
        )
        return 1

    out_path = Path(args.output)
    out_path.write_text(fresh, encoding="utf-8")
    print(f"wrote {out_path}  ({len(registry)} routes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
