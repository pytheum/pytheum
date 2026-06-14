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

## Quickstart — standalone offline serve

`pytheum serve` starts an offline HTTP API from the bundled datasets.
No database, no API keys, no network access required.

```bash
pip install pytheum
pytheum serve              # binds http://127.0.0.1:8080
pytheum serve --port 9090  # custom port
pytheum serve --mcp        # also start MCP server on port 8444
```

On startup the banner prints which routes are live vs degraded:

```
============================================================
  pytheum 0.0.1 — offline serve
============================================================
  HTTP API:  http://127.0.0.1:8080

  Bundled datasets:
    equivalence pairs : 136,877
    related pairs     : 1,097
    dataset version   : 2026-06-12T21:40:00Z

  Live routes (bundled data):
    GET /v1/status
    GET /v1/markets/equivalents
    GET /v1/markets/matched
    GET /v1/markets/{ref}/equivalents
    GET /v1/markets/{ref}/rules
    GET /v1/markets/{ref}/related
    GET /llms.txt
    GET /healthz
...
```

### Example API call

```bash
# Look up the Polymarket equivalent of a Kalshi market
curl http://127.0.0.1:8080/v1/markets/kalshi:COSTCOHOTDOG-27/equivalents

# Browse 136k+ verified pairs, filtered by sport
curl "http://127.0.0.1:8080/v1/markets/matched?bet_type=sports&limit=10"
```

### MCP connector (Claude / Cursor / etc.)

Start with `--mcp` and point your MCP client at the printed URL:

```bash
pytheum serve --mcp
# MCP:  http://127.0.0.1:8444/mcp  (streamable-HTTP)
```

Add to your MCP client config:

```json
{
  "mcpServers": {
    "pytheum": {
      "url": "http://127.0.0.1:8444/mcp"
    }
  }
}
```

Then call any tool, e.g.:

```
find_equivalent kalshi:COSTCOHOTDOG-27
```

Full tool inventory: `GET http://127.0.0.1:8080/llms.txt`

### Hosted MCP (always-on)

```
https://api.pytheum.com/mcp
```

---

## Library usage

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
