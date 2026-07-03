# pytheum — Python SDK

A typed, concurrency-optimized Python client for the [pytheum](https://api.pytheum.com)
REST API: the settlement-verified cross-venue prediction-market graph (Kalshi ×
Polymarket), live prices, and real-time context. The same 27 capabilities the MCP
server exposes, as a library.

```python
pip install pytheum        # httpx is the only runtime dep
```

## Quickstart

```python
import asyncio
from pytheum.client import AsyncClient

async def main():
    async with AsyncClient() as px:                 # single pooled client
        st = await px.status()
        print(st["equivalence"]["pairs_loaded"])    # 142_941

        # the arb radar — matched pairs ranked by executable, fee-netted edge
        radar = await px.find_divergences(limit=20)

        # fan out safely: the governor bounds real in-flight requests
        refs = [p["kalshi"]["id"] for p in radar["pairs"]]
        cores = await px.get_markets(refs)          # concurrent, rate-safe

asyncio.run(main())
```

Synchronous is a first-class, real client (not asyncio-wrapped):

```python
from pytheum.client import Client

with Client() as px:
    for m in px.search("bitcoin", limit=10)["markets"]:
        print(m["question"], m["volume_usd"])
```

## Why it's production-grade

- **Connection pooling** — one reused `httpx` client per instance.
- **Concurrency governor** — an in-flight semaphore (default 8) keeps a caller inside
  the API's per-IP envelope while maximizing throughput; fan out freely.
- **Transparent resilience** — bounded retries with exponential backoff + full jitter;
  429s honor `Retry-After`; timeouts / 5xx retried. Your code rarely sees a transient error.
- **Typed** — every route is a typed method; optional dataclass models via
  `pytheum.client.models`.
- **Lean** — `import pytheum.client` pulls no server code; httpx + stdlib only.

## Typed responses (optional)

Methods return the parsed JSON payload (forward-compatible; new API fields just appear).
For typed, attribute-access objects, wrap a payload in a model:

```python
from pytheum.client import Client, models

st = models.Status.from_dict(Client().status())
st.equivalence_pairs_loaded     # 142_941
st.platforms["kalshi"].markets  # typed; st.raw keeps the original dict
```

`from_dict` is lenient: unknown keys are ignored, missing keys tolerated, and the full
original payload is preserved on `.raw`.

## Tuning

```python
AsyncClient(
    base_url="https://api.pytheum.com",
    max_concurrency=8,        # in-flight governor
    max_retries=3,            # per request
    connect_timeout=5.0, read_timeout=30.0,
    max_connections=20, max_keepalive=10,
)
```

## Errors

All derive from `PytheumError`:
`RateLimitError` (429, carries `.retry_after`), `NotFoundError` (404),
`AuthError` (401/403), `ServerError` (5xx), `PytheumTimeout`, `ConnectionFailed`, and
the base `APIError` (carries `.status`, `.body`, `.hint`).

## Surface

Meta: `status` · `quality` · `about` · `guide`.
Discovery: `search` · `screen` · `get_market`.
Cross-venue graph: `equivalents` · `matched_pairs` · `find_divergences` · `related` · `rules`.
Context: `context` · `bundle_context` · `context_batch` · `event_related_markets`.
Market data: `orderbook` · `trades` · `ohlcv` · `open_interest` · `history` · `flow`.
Trader intel: `leaderboard` · `trader` · `holders` · `whale_trades`.
Batch helpers: `gather` · `get_markets` · `equivalents_many`.

The public edge is keyless (per-IP rate-limited); pass `api_key=...` only if your account
requires one.
