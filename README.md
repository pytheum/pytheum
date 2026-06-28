# pytheum

**The verified prediction-market graph — ~140k+ settlement-verified
Kalshi×Polymarket pairs; 1.6M verified connections across 364k markets.**

Every pair is verified by **settlement semantics** — the two markets resolve to
the same real-world outcome — not by fuzzy title similarity. The matching is
deterministic and inspectable, and we publish the precision methodology. As of
our 2026-06-12 competitor sweep, no other prediction-market matcher does.

Real-time prediction-market intelligence API: verified Kalshi↔Polymarket
equivalence data, live orderbook quotes, news/social context, and trader
analytics. This repository is the public serve-side library (`pytheum`); the
private process (`pytheum-pit`) imports it and registers PIT-only routes on top.

---

## Benchmarks

We benchmarked the live cross-venue matchers hands-on (published SDKs / free-tier
APIs, 2026-06-12) against a **stratified golden set of 50 verified pairs across 5
bet-type groups plus a constructed trap taxonomy**. The headline finding: the
commercial matchers do not return matches through their published interfaces, and
none publish a precision number.

| Matcher | Cross-venue matching (measured) | Precision published? | Self-hostable? |
|---|---|---|---|
| **pytheum** | ~140k+ settlement-verified pairs; deterministic structured keys + gates | **Yes** — golden set + trap taxonomy | **Yes** (self-host the SDK against your own export) |
| PMXT Router (v2.49.9 SDK) | **0/150 recall** — per-market lookup is degenerate (returns a constant popular-markets list regardless of input); bulk catalog real but shallow (7/100 clusters game-like, no structured bet-type depth) | No | No (hosted-only) |
| Prediction Hunt (free tier) | **`success:false`, `count:0` on every query** (HTTP 200, no error code) — could not extract one match | No | No |
| Polymarket/Dome | Acquired and discontinued (EOL 2026-04-28; matching endpoints 404) | — | — |
| Oddpool | ~750 static event groupings; no methodology | No | No |

*Measured through published Python SDKs / free-tier APIs on 2026-06-12; paid
tiers and hosted MCPs were not separately tested. This is, to our knowledge, the
first precision measurement of any commercial prediction-market matcher. The
golden-set + trap methodology is reusable — re-run on revisits.*

Where pytheum's depth lives: **133k+ of the verified pairs are structured-sports
matches** (totals, spreads, props, tennis, esports, moneyline) produced by
deterministic keys — exactly the bet-type depth the commercial catalogs lack.

---

## Install

```bash
pip install pytheum
```

Requires Python ≥ 3.11.

---

## What you get

This package is the **client / SDK + MCP server** for the pytheum prediction-market
graph. It ships **no data** — it talks to the hosted API at
**`https://api.pytheum.com`** by default, or to a local equivalence/related export
that you provide for self-hosting (set `PYTHEUM_EQUIVALENCE_PATH` /
`PYTHEUM_RELATED_PATH`). The pair-matching methodology and benchmarks below
describe the data the hosted graph serves.

### Easiest path — point an MCP client at the hosted server

No install required. Add to your MCP client config (Claude / Cursor / etc.):

```json
{
  "mcpServers": {
    "pytheum": {
      "url": "https://api.pytheum.com/mcp"
    }
  }
}
```

Then call any tool, e.g.:

```
t_equivalent_markets kalshi:COSTCOHOTDOG-27
```

### Self-host the serve stack

`pytheum serve` starts a local HTTP API (plus an optional MCP server). With no
dataset configured the equivalence/related routes return empty results and the
live-venue routes degrade gracefully (no secrets required). To serve real pairs,
point it at a local export.

```bash
pip install pytheum

# Serve against the hosted graph's MCP surface, or self-host with your own export:
export PYTHEUM_EQUIVALENCE_PATH=/path/to/equivalence-export.jsonl.gz
export PYTHEUM_RELATED_PATH=/path/to/related-export.jsonl.gz

pytheum serve              # binds http://127.0.0.1:8080
pytheum serve --port 9090  # custom port
pytheum serve --mcp        # also start the MCP server on port 8444
```

On startup the banner prints the loaded pair counts (0 when no export is
configured) and which routes are live vs degraded:

```
============================================================
  pytheum 0.1.0 — serve
============================================================
  HTTP API:  http://127.0.0.1:8080

  Datasets (from PYTHEUM_EQUIVALENCE_PATH / PYTHEUM_RELATED_PATH):
    equivalence pairs : 141,844
    related pairs     : 1,097

  Live routes (equivalence/related data):
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

# Browse the verified pairs, filtered by sport
curl "http://127.0.0.1:8080/v1/markets/matched?bet_type=sports&limit=10"
```

### MCP connector (self-hosted)

Start with `--mcp` and point your MCP client at the printed URL:

```bash
pytheum serve --mcp
# MCP:  http://127.0.0.1:8444/mcp  (streamable-HTTP)
```

```json
{
  "mcpServers": {
    "pytheum": {
      "url": "http://127.0.0.1:8444/mcp"
    }
  }
}
```

Full tool inventory: `GET http://127.0.0.1:8080/llms.txt`

---

## Tools / routes

The self-hosted serve exposes the equivalence/related routes; the full hosted API
adds the live trader-analytics and OHLCV tiers. Core surface:

| Route | Returns |
|---|---|
| `GET /v1/markets/matched` | Browse verified cross-venue pairs (filter by `bet_type`, `league`, `date`) |
| `GET /v1/markets/{ref}/equivalents` | Settlement-verified equivalents for one market |
| `GET /v1/markets/{ref}/related` | Correlated (non-equivalent) pairs — same asset/event, different band or deadline |
| `GET /v1/markets/{ref}/rules` | Resolution-rule text for both legs (the verifiability surface) |
| `GET /v1/markets/{ref}/book`, `/oi`, `/ohlcv` | Live orderbook, open interest, candles (hosted tier) |
| `GET /llms.txt` | Machine-readable tool inventory for agents |

Full schema: [`openapi.yaml`](openapi.yaml).

---

## How matching works / why it's verifiable

pytheum does **not** match on title or embedding similarity. Both venues encode
each market in a structured ID (Kalshi `event_ticker`, Polymarket `slug`), so the
matcher keys on the underlying facts — (league/tour, date, teams/players, line,
direction) for sports; category token-sets and resolution magnitudes for awards,
elections, and macro. Each pair must clear deterministic **gates** before it
ships:

- **1:1 integrity** — every market appears in at most one verified pair.
- **Line / name-alignment invariants** — strike, threshold, and entity tokens
  must agree on both legs (no single-anchor accepts).
- **Settlement-fungibility** — both legs must resolve to the same real-world
  outcome, not merely describe the same topic.

Because the keys are deterministic and the gates are explicit, every match is
**inspectable and reproducible** — you can read the rule text on both legs
(`/rules`) and see why two markets are the same bet. That auditability is the
differentiator: fuzzy similarity engines cannot tell you why, and cannot be
re-derived.

---

## Library usage

```python
from pytheum.registry import RouterRegistry, RouteSpec
from pytheum.routing import RouterApp

registry = RouterRegistry()

async def handle_status(query: dict) -> tuple[int, dict]:
    return 200, {"status": "ok", "pairs": 141844}

registry.add(RouteSpec("GET", "/v1/status", handle_status, summary="Health check"))

app = RouterApp(registry.build_router())
# Pass `app` to uvicorn or serve_embedded()
```

---

## License

- **Code: MIT** (see [`LICENSE`](LICENSE)).
- **Data (`datasets/`): CC-BY-4.0** — the settlement-verified cross-venue pair
  artifacts. Formal attribution text is pinned at the v0.1.0 data release.

See [`datasets/README.md`](datasets/README.md) for the artifact schema and
checksums.

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
</content>
