# pytheum

**The verified prediction-market graph — 1.6M verified connections across
142k markets; 136,877 settlement-verified cross-venue pairs.**

Real-time prediction-market intelligence API: verified Kalshi↔Polymarket
equivalence data, live orderbook quotes, news/social context, and trader
analytics.  This repository is the public serve-side library (`pytheum`);
the private process (`pytheum-pit`) imports it and registers PIT-only routes
on top.

---

## Benchmarks

| Benchmark | Score | Scorecard |
|---|---|---|
| PMXT (cross-venue pair precision) | — | [PMXT scorecard](docs/PMXT.md) |
| PH (settlement-verified recall) | — | [PH scorecard](docs/PH.md) |

*Scorecards populated at v0.1.0 release.*

---

## Install

```bash
pip install pytheum
```

Requires Python ≥ 3.11.

---

## Quickstart

```python
from pytheum.registry import RouterRegistry, RouteSpec
from pytheum.routing import RouterApp

registry = RouterRegistry()

async def handle_status(query: dict) -> tuple[int, dict]:
    return 200, {"status": "ok", "pairs": 136877}

registry.add(RouteSpec("GET", "/v1/status", handle_status, summary="Health check"))

app = RouterApp(registry.build_router())
# Pass `app` to uvicorn or serve_embedded()
```

---

## MCP connector

```
https://api.pytheum.com/mcp
```

The MCP server exposes 22 tools covering equivalence lookup, market context,
orderbook quotes, trader analytics, and OHLCV candles.  Full tool inventory
available at [`/llms.txt`](https://api.pytheum.com/llms.txt).

---

## Dataset license

The `datasets/` directory ships settlement-verified cross-venue pair data.

**License: CC-BY-4.0 (TBD marker — formal license text and attribution
requirements will be pinned at v0.1.0 data release).**

See [`datasets/README.md`](datasets/README.md) for the artifact schema.

---

## Development

```bash
git clone https://github.com/pytheum/pytheum
cd pytheum
uv sync --extra dev
uv run pytest
```

CI gates: ruff lint, mypy --strict, pytest (cov ≥ 80%), openapi drift check,
dataset checksum verification.
