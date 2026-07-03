#!/usr/bin/env python3
"""stress_ramp.py — disciplined, self-limiting concurrency ramp against the LIVE
pytheum REST API (https://api.pytheum.com, fronted by Caddy, documented per-IP
limit 100 req/s) ahead of customer outreach.

This is NOT a "hammer prod" load test. It is a gradual ramp with a hard
per-level abort rule (>20% non-200 responses stops the ramp for that endpoint
right there) plus a broad-distress abort for the whole run (many endpoints
throwing 5xx simultaneously outside the known, self-recovering export-swap
502 window).

Four phases, run in order:

  1. REST ramp — for each hot endpoint, run concurrency levels [5, 10, 20, 40]
     for 30s each (open-loop-ish: N worker coroutines, request -> 50ms think
     time -> repeat). 3s pause between levels, 10s pause between endpoints.
  2. Limiter provocation — a single unpaced burst of 150 requests to
     /v1/status only, to deliberately trip the edge rate limiter. Clean 429s
     are a PASS. Verifies 200s resume within ~5s after the burst.
  3. MCP cold-spike re-verify — reuses the *exact* MCPClient driver from
     scripts/bench_mcp.py (do not reimplement the protocol), opens a genuinely
     fresh session, and times a cold + 10 warm calls each for t_screen and
     t_find_divergences.
  4. 502-window scan — every response gathered in steps 1-3 is scanned for 5xx
     bursts; reported with wall-clock timestamps regardless of phase.

Usage:
    python -m scripts.stress_ramp [--base-url https://api.pytheum.com]
                                   [--mcp-base https://mcp.pytheum.com/]
                                   [--out-json PATH]

Dependencies: stdlib + httpx (already a project dependency). Reuses
scripts/bench_mcp.py's MCPClient for the MCP phase.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

# Make `from scripts.bench_mcp import MCPClient` work whether invoked as
# `python -m scripts.stress_ramp` (repo root on sys.path) or
# `python scripts/stress_ramp.py` (scripts/ dir on sys.path) — reuse the
# exact, already-working MCP driver rather than reimplementing the protocol.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))
from scripts.bench_mcp import MCPClient, RateLimited  # noqa: E402

DEFAULT_BASE_URL = "https://api.pytheum.com"
DEFAULT_MCP_BASE = "https://mcp.pytheum.com/"

CONCURRENCY_LEVELS = [5, 10, 20, 40]
LEVEL_DURATION_S = 30.0
THINK_TIME_S = 0.05
LEVEL_GAP_S = 3.0
ENDPOINT_GAP_S = 10.0
HARD_STOP_ERROR_FRACTION = 0.20
REQUEST_TIMEOUT_S = 20.0

BROAD_DISTRESS_ENDPOINT_THRESHOLD = 3  # >=N distinct endpoints
BROAD_DISTRESS_WINDOW_S = 10.0  # ...within this many seconds of each other...
BROAD_DISTRESS_5XX_FRACTION = 0.30  # ...each with >=this fraction 5xx in-window

ENDPOINTS: list[dict[str, Any]] = [
    {"name": "status", "path": "/v1/status", "params": {}},
    {"name": "markets_search", "path": "/v1/markets/search", "params": {"q": "bitcoin", "limit": 50}},
    {"name": "markets_screen", "path": "/v1/markets/screen", "params": {"limit": 50}},
    {"name": "markets_matched", "path": "/v1/markets/matched", "params": {"limit": 50}},
    {"name": "markets_equivalents", "path": "/v1/markets/equivalents", "params": {"limit": 150}},
]


# --------------------------------------------------------------------------
# Shared records
# --------------------------------------------------------------------------


@dataclass
class Sample:
    t_wall: float  # time.time() epoch seconds, for 502-window correlation
    status: int  # 0 = network error/timeout
    elapsed_ms: float
    endpoint: str
    phase: str
    error: str | None = None


ALL_SAMPLES: list[Sample] = []  # global sink used by the 502-window scan


def percentile(values: list[float], pct: float) -> float | None:
    if not values:
        return None
    s = sorted(values)
    k = (len(s) - 1) * pct
    f, c = int(k), min(int(k) + 1, len(s) - 1)
    if f == c:
        return s[f]
    return s[f] + (s[c] - s[f]) * (k - f)


async def _one_get(client: httpx.AsyncClient, path: str, params: dict[str, Any], endpoint: str, phase: str) -> Sample:
    t_wall = time.time()
    t0 = time.perf_counter()
    try:
        resp = await client.get(path, params=params, timeout=REQUEST_TIMEOUT_S)
        elapsed_ms = (time.perf_counter() - t0) * 1000.0
        s = Sample(t_wall=t_wall, status=resp.status_code, elapsed_ms=elapsed_ms, endpoint=endpoint, phase=phase)
    except Exception as exc:  # network error / timeout
        elapsed_ms = (time.perf_counter() - t0) * 1000.0
        s = Sample(t_wall=t_wall, status=0, elapsed_ms=elapsed_ms, endpoint=endpoint, phase=phase, error=repr(exc))
    ALL_SAMPLES.append(s)
    return s


# --------------------------------------------------------------------------
# Phase 1 — REST ramp
# --------------------------------------------------------------------------


@dataclass
class LevelResult:
    concurrency: int
    total: int
    ok200: int
    n429: int
    n5xx: int
    n_other_err: int
    p50: float | None
    p95: float | None
    p99: float | None
    max_ms: float | None
    error_fraction: float
    hard_stopped: bool
    hard_stop_reason: str = ""


@dataclass
class EndpointRampResult:
    name: str
    path: str
    levels: list[LevelResult] = field(default_factory=list)
    aborted_after: int | None = None  # concurrency level at which we stopped, if any


async def run_level(
    client: httpx.AsyncClient, path: str, params: dict[str, Any], endpoint: str, concurrency: int, duration_s: float
) -> LevelResult:
    """N worker coroutines: request -> 50ms think -> repeat, for duration_s wall clock."""
    deadline = time.perf_counter() + duration_s
    samples: list[Sample] = []
    lock = asyncio.Lock()

    async def worker() -> None:
        while time.perf_counter() < deadline:
            s = await _one_get(client, path, params, endpoint, phase="ramp")
            async with lock:
                samples.append(s)
            await asyncio.sleep(THINK_TIME_S)

    await asyncio.gather(*(worker() for _ in range(concurrency)))

    total = len(samples)
    ok200 = sum(1 for s in samples if s.status == 200)
    n429 = sum(1 for s in samples if s.status == 429)
    n5xx = sum(1 for s in samples if 500 <= s.status < 600)
    n_other_err = total - ok200 - n429 - n5xx
    non200 = total - ok200
    error_fraction = (non200 / total) if total else 0.0
    lat = [s.elapsed_ms for s in samples if s.status == 200]

    return LevelResult(
        concurrency=concurrency,
        total=total,
        ok200=ok200,
        n429=n429,
        n5xx=n5xx,
        n_other_err=n_other_err,
        p50=percentile(lat, 0.50),
        p95=percentile(lat, 0.95),
        p99=percentile(lat, 0.99),
        max_ms=max(lat) if lat else None,
        error_fraction=error_fraction,
        hard_stopped=False,
    )


async def run_endpoint_ramp(client: httpx.AsyncClient, spec: dict[str, Any]) -> EndpointRampResult:
    result = EndpointRampResult(name=spec["name"], path=spec["path"])
    for i, level in enumerate(CONCURRENCY_LEVELS):
        print(
            f"  [{spec['name']}] concurrency={level} for {LEVEL_DURATION_S:.0f}s ...",
            flush=True,
        )
        lvl = await run_level(client, spec["path"], spec["params"], spec["name"], level, LEVEL_DURATION_S)
        rate = lvl.total / LEVEL_DURATION_S
        print(
            f"    -> total={lvl.total} ({rate:.1f} req/s) ok200={lvl.ok200} 429={lvl.n429} 5xx={lvl.n5xx} "
            f"other={lvl.n_other_err} err_frac={lvl.error_fraction:.1%} "
            f"p50={lvl.p50:.0f}ms p95={lvl.p95 if lvl.p95 is None else f'{lvl.p95:.0f}ms'} "
            f"p99={lvl.p99 if lvl.p99 is None else f'{lvl.p99:.0f}ms'}"
            if lvl.p50 is not None
            else f"    -> total={lvl.total} ({rate:.1f} req/s) ok200=0 429={lvl.n429} 5xx={lvl.n5xx} "
            f"other={lvl.n_other_err} err_frac={lvl.error_fraction:.1%} (no successful latency samples)",
            flush=True,
        )
        if lvl.error_fraction > HARD_STOP_ERROR_FRACTION:
            lvl.hard_stopped = True
            lvl.hard_stop_reason = (
                f"{level} concurrency -> {lvl.error_fraction:.0%} non-200 "
                f"(429={lvl.n429}, 5xx={lvl.n5xx}, other={lvl.n_other_err}) — exceeded "
                f"{HARD_STOP_ERROR_FRACTION:.0%} hard-stop threshold, aborting ramp for this endpoint"
            )
            print(f"    HARD STOP: {lvl.hard_stop_reason}", flush=True)
            result.levels.append(lvl)
            result.aborted_after = level
            return result
        result.levels.append(lvl)
        if i < len(CONCURRENCY_LEVELS) - 1:
            await asyncio.sleep(LEVEL_GAP_S)
    return result


def check_broad_distress(samples: list[Sample], window_end: float) -> str | None:
    """Scan samples up to window_end for multi-endpoint simultaneous 5xx distress,
    as distinct from the known, isolated, self-recovering export-swap 502 window.
    Returns a human-readable description if broad distress is detected, else None."""
    recent = [s for s in samples if s.status >= 500 and s.t_wall <= window_end]
    if not recent:
        return None
    # Bucket 5xx samples into BROAD_DISTRESS_WINDOW_S-wide sliding windows and check
    # how many distinct endpoints show >=BROAD_DISTRESS_5XX_FRACTION 5xx-rate within it.
    recent.sort(key=lambda s: s.t_wall)
    by_time = recent
    for i, s in enumerate(by_time):
        w_start, w_end = s.t_wall, s.t_wall + BROAD_DISTRESS_WINDOW_S
        in_window = [x for x in samples if w_start <= x.t_wall <= w_end]
        endpoints_in_window = {x.endpoint for x in in_window}
        distressed_endpoints = set()
        for ep in endpoints_in_window:
            ep_samples = [x for x in in_window if x.endpoint == ep]
            if not ep_samples:
                continue
            frac_5xx = sum(1 for x in ep_samples if x.status >= 500) / len(ep_samples)
            if frac_5xx >= BROAD_DISTRESS_5XX_FRACTION:
                distressed_endpoints.add(ep)
        if len(distressed_endpoints) >= BROAD_DISTRESS_ENDPOINT_THRESHOLD:
            return (
                f"Broad multi-endpoint 5xx distress detected: {len(distressed_endpoints)} endpoints "
                f"({sorted(distressed_endpoints)}) each with >={BROAD_DISTRESS_5XX_FRACTION:.0%} 5xx-rate "
                f"within a {BROAD_DISTRESS_WINDOW_S:.0f}s window starting {time.strftime('%H:%M:%S', time.gmtime(w_start))} UTC"
            )
    return None


async def phase1_rest_ramp(client: httpx.AsyncClient) -> tuple[list[EndpointRampResult], bool, str]:
    """Returns (results, aborted_whole_run, abort_reason)."""
    results: list[EndpointRampResult] = []
    for i, spec in enumerate(ENDPOINTS):
        print(f"\n=== Phase 1: REST ramp — {spec['name']} ({spec['path']}) ===", flush=True)
        res = await run_endpoint_ramp(client, spec)
        results.append(res)

        distress = check_broad_distress(ALL_SAMPLES, window_end=time.time())
        if distress:
            return results, True, distress

        if i < len(ENDPOINTS) - 1:
            await asyncio.sleep(ENDPOINT_GAP_S)
    return results, False, ""


# --------------------------------------------------------------------------
# Phase 2 — limiter provocation (/v1/status burst)
# --------------------------------------------------------------------------


@dataclass
class BurstResult:
    total: int
    n200: int
    n429: int
    n5xx: int
    n_other: int
    sample_429_body: str | None
    sample_429_headers: dict[str, str] | None
    recovery_s: float | None  # seconds after burst end until 200s resume; None if never / immediate


async def phase2_limiter_burst(client: httpx.AsyncClient) -> BurstResult:
    print("\n=== Phase 2: limiter provocation — 150-request unpaced burst on /v1/status ===", flush=True)
    path = "/v1/status"

    async def one() -> tuple[int, str | None, dict[str, str] | None]:
        try:
            resp = await client.get(path, timeout=REQUEST_TIMEOUT_S)
            ALL_SAMPLES.append(
                Sample(
                    t_wall=time.time(),
                    status=resp.status_code,
                    elapsed_ms=0.0,
                    endpoint="status_burst",
                    phase="burst",
                )
            )
            body = resp.text if resp.status_code == 429 else None
            headers = dict(resp.headers) if resp.status_code == 429 else None
            return resp.status_code, body, headers
        except Exception as exc:
            ALL_SAMPLES.append(
                Sample(
                    t_wall=time.time(),
                    status=0,
                    elapsed_ms=0.0,
                    endpoint="status_burst",
                    phase="burst",
                    error=repr(exc),
                )
            )
            return 0, None, None

    burst_end_t0 = time.perf_counter()
    outcomes = await asyncio.gather(*(one() for _ in range(150)))
    burst_wall_end = time.time()

    n200 = sum(1 for o in outcomes if o[0] == 200)
    n429 = sum(1 for o in outcomes if o[0] == 429)
    n5xx = sum(1 for o in outcomes if 500 <= o[0] < 600)
    n_other = len(outcomes) - n200 - n429 - n5xx

    sample_body, sample_headers = None, None
    for status, body, headers in outcomes:
        if status == 429 and body is not None:
            sample_body, sample_headers = body, headers
            break

    print(
        f"  burst done in {time.perf_counter() - burst_end_t0:.2f}s: 200={n200} 429={n429} 5xx={n5xx} other={n_other}",
        flush=True,
    )

    # Poll for 200s to resume, up to ~8s (a bit beyond the ~5s spec target so we
    # can report an honest number even if it runs slightly long).
    recovery_s: float | None = None
    poll_start = time.perf_counter()
    while time.perf_counter() - poll_start < 8.0:
        try:
            resp = await client.get(path, timeout=REQUEST_TIMEOUT_S)
            ALL_SAMPLES.append(
                Sample(time.time(), resp.status_code, 0.0, "status_burst_recovery", "burst")
            )
            if resp.status_code == 200:
                recovery_s = time.time() - burst_wall_end
                break
        except Exception:
            pass
        await asyncio.sleep(0.25)

    print(
        f"  recovery: 200 resumed {recovery_s:.2f}s after burst end"
        if recovery_s is not None
        else "  recovery: did NOT observe a 200 within 8s of burst end",
        flush=True,
    )

    return BurstResult(
        total=len(outcomes),
        n200=n200,
        n429=n429,
        n5xx=n5xx,
        n_other=n_other,
        sample_429_body=sample_body,
        sample_429_headers=sample_headers,
        recovery_s=recovery_s,
    )


# --------------------------------------------------------------------------
# Phase 3 — MCP cold-spike re-verify (reuses scripts/bench_mcp.py's MCPClient)
# --------------------------------------------------------------------------


@dataclass
class MCPToolResult:
    tool: str
    cold_ms: float | None
    warm_ms: list[float]
    warm_p95: float | None
    error: str | None = None


def phase3_mcp_coldspike(mcp_base: str) -> list[MCPToolResult]:
    print("\n=== Phase 3: MCP cold-spike re-verify (t_screen, t_find_divergences) ===", flush=True)
    print("  opening a fresh MCP session (not reused from the REST ramp) ...", flush=True)
    client = MCPClient(base=mcp_base)
    client.initialize()
    print(f"  session={client.session_id}", flush=True)

    # Minimal live refs for args, mirroring bench_mcp.py's discover_refs (kept
    # small/scoped here since we only need args for these two tools).
    call_args = {
        "t_screen": {"sort_by": "move", "limit": 3},
        "t_find_divergences": {"min_net_edge": 0.0, "limit": 3},
    }

    results: list[MCPToolResult] = []
    for tool, args in call_args.items():
        print(f"  {tool}: cold call ...", flush=True)
        try:
            t0 = time.perf_counter()
            client.call(tool, args)
            cold_ms = (time.perf_counter() - t0) * 1000.0
        except RateLimited as e:
            results.append(MCPToolResult(tool=tool, cold_ms=None, warm_ms=[], warm_p95=None, error=str(e)))
            print(f"    RATE LIMITED on cold call: {e}", flush=True)
            continue
        except Exception as e:
            results.append(
                MCPToolResult(tool=tool, cold_ms=None, warm_ms=[], warm_p95=None, error=f"{type(e).__name__}: {e}")
            )
            print(f"    ERROR on cold call: {e}", flush=True)
            continue
        print(f"    cold={cold_ms:.1f}ms", flush=True)

        warm_ms: list[float] = []
        err: str | None = None
        for i in range(10):
            try:
                t0 = time.perf_counter()
                client.call(tool, args)
                warm_ms.append((time.perf_counter() - t0) * 1000.0)
            except RateLimited as e:
                err = f"rate limited on warm call {i}: {e}"
                print(f"    {err}", flush=True)
                break
            except Exception as e:
                err = f"error on warm call {i}: {type(e).__name__}: {e}"
                print(f"    {err}", flush=True)
                break
        warm_p95 = percentile(warm_ms, 0.95)
        print(f"    warm n={len(warm_ms)} p95={warm_p95:.1f}ms" if warm_p95 is not None else "    warm: no samples", flush=True)
        results.append(MCPToolResult(tool=tool, cold_ms=cold_ms, warm_ms=warm_ms, warm_p95=warm_p95, error=err))

    return results


# --------------------------------------------------------------------------
# Phase 4 — 502-window scan (over ALL_SAMPLES, all phases)
# --------------------------------------------------------------------------


@dataclass
class FiveXXWindow:
    start_wall: float
    end_wall: float
    duration_s: float
    endpoints: list[str]
    count: int


def phase4_scan_5xx_windows(samples: list[Sample], gap_s: float = 15.0) -> list[FiveXXWindow]:
    fivexx = sorted([s for s in samples if s.status >= 500], key=lambda s: s.t_wall)
    if not fivexx:
        return []
    windows: list[FiveXXWindow] = []
    cur: list[Sample] = [fivexx[0]]
    for s in fivexx[1:]:
        if s.t_wall - cur[-1].t_wall <= gap_s:
            cur.append(s)
        else:
            windows.append(_mk_window(cur))
            cur = [s]
    windows.append(_mk_window(cur))
    return windows


def _mk_window(group: list[Sample]) -> FiveXXWindow:
    return FiveXXWindow(
        start_wall=group[0].t_wall,
        end_wall=group[-1].t_wall,
        duration_s=group[-1].t_wall - group[0].t_wall,
        endpoints=sorted({s.endpoint for s in group}),
        count=len(group),
    )


# --------------------------------------------------------------------------
# Report rendering
# --------------------------------------------------------------------------


def _fmt_ms(v: float | None) -> str:
    return "-" if v is None else f"{v:.0f}"


def render_report(
    *,
    base_url: str,
    mcp_base: str,
    started_at: str,
    duration_s: float,
    ramp_results: list[EndpointRampResult],
    aborted_whole_run: bool,
    abort_reason: str,
    burst: BurstResult,
    mcp_results: list[MCPToolResult],
    fivexx_windows: list[FiveXXWindow],
) -> str:
    lines: list[str] = []
    lines.append("# Stress ramp — customer-outreach readiness (2026-07-03)")
    lines.append("")
    lines.append(f"- **Target**: `{base_url}` (fronted by Caddy, documented per-IP limit 100 req/s, 16GB box)")
    lines.append(f"- **MCP target**: `{mcp_base}`")
    lines.append(f"- **Run started**: {started_at}")
    lines.append(f"- **Total runtime**: {duration_s / 60:.1f} min ({duration_s:.0f}s)")
    lines.append("- **Script**: `scripts/stress_ramp.py`")
    lines.append(
        "- **Method**: gradual, self-limiting concurrency ramp. Per endpoint, 30s at each "
        "concurrency level `[5, 10, 20, 40]` (N worker coroutines: request -> 50ms think time -> "
        "repeat), 3s between levels, 10s between endpoints. **Hard rule**: any level with >20% "
        "non-200 responses aborts the ramp for that endpoint (no higher levels attempted). A "
        "separate whole-run abort rule fires on broad multi-endpoint 5xx distress."
    )
    if aborted_whole_run:
        lines.append("")
        lines.append(f"## RUN ABORTED EARLY — {abort_reason}")
    lines.append("")

    lines.append("## Phase 1 — REST ramp results")
    lines.append("")
    lines.append(
        "| Endpoint | Concurrency | req/s | ok(200) | 429 | 5xx | other | error% | p50 | p95 | p99 | max |"
    )
    lines.append("|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|")
    for res in ramp_results:
        for lvl in res.levels:
            rate = lvl.total / LEVEL_DURATION_S
            stop_marker = " **HARD-STOP**" if lvl.hard_stopped else ""
            lines.append(
                f"| `{res.path}` | {lvl.concurrency}{stop_marker} | {rate:.1f} | {lvl.ok200} | {lvl.n429} | "
                f"{lvl.n5xx} | {lvl.n_other_err} | {lvl.error_fraction:.1%} | {_fmt_ms(lvl.p50)}ms | "
                f"{_fmt_ms(lvl.p95)}ms | {_fmt_ms(lvl.p99)}ms | {_fmt_ms(lvl.max_ms)}ms |"
            )
    lines.append("")

    lines.append("### Hard-stops")
    lines.append("")
    any_stop = False
    for res in ramp_results:
        for lvl in res.levels:
            if lvl.hard_stopped:
                any_stop = True
                lines.append(f"- `{res.path}`: {lvl.hard_stop_reason}")
    if not any_stop:
        lines.append("None — every endpoint completed all four concurrency levels within the error budget.")
    lines.append("")

    lines.append("### Max SAFE concurrency per endpoint")
    lines.append("")
    lines.append(
        "Defined as: the last tested level with <1% errors AND p99 < 2000ms. If no level meets "
        "both, that's stated explicitly."
    )
    lines.append("")
    for res in ramp_results:
        safe_levels = [lvl for lvl in res.levels if lvl.error_fraction < 0.01 and lvl.p99 is not None and lvl.p99 < 2000.0]
        if res.aborted_after is not None:
            note = f" (ramp hard-stopped at concurrency={res.aborted_after})"
        else:
            note = ""
        if safe_levels:
            best = max(safe_levels, key=lambda lvl: lvl.concurrency)
            lines.append(
                f"- `{res.path}`: **{best.concurrency}** (p99={_fmt_ms(best.p99)}ms, error={best.error_fraction:.2%}){note}"
            )
        else:
            lines.append(f"- `{res.path}`: **no level was clean** (<1% err AND p99<2s) among those tested{note}")
    lines.append("")

    lines.append("## Phase 2 — limiter provocation (`/v1/status`, 150-request unpaced burst)")
    lines.append("")
    lines.append(f"- Total: {burst.total}, 200: {burst.n200}, 429: {burst.n429}, 5xx: {burst.n5xx}, other: {burst.n_other}")
    verdict = "PASS (clean 429s observed)" if burst.n429 > 0 and burst.n5xx == 0 else (
        "AMBIGUOUS — no 429s observed (limiter may not have engaged / status is keyless-exempt at the app layer, Caddy edge limit may sit above 150 in this window)"
        if burst.n5xx == 0
        else "FAIL — 5xx observed during burst, not clean rate-limiting"
    )
    lines.append(f"- **Limiter verdict**: {verdict}")
    if burst.sample_429_headers is not None:
        retry_after = burst.sample_429_headers.get("retry-after") or burst.sample_429_headers.get("Retry-After")
        lines.append(f"- Sample 429 `Retry-After` header: `{retry_after}`")
        lines.append(f"- Sample 429 headers: `{json.dumps(burst.sample_429_headers)}`")
        lines.append(f"- Sample 429 body: `{(burst.sample_429_body or '')[:500]}`")
    else:
        lines.append("- No 429 response captured to sample (see verdict above).")
    if burst.recovery_s is not None:
        lines.append(f"- **Recovery**: 200s resumed **{burst.recovery_s:.2f}s** after burst end.")
    else:
        lines.append("- **Recovery**: did NOT observe a 200 within the 8s poll window after burst end.")
    lines.append("")

    lines.append("## Phase 3 — MCP cold-spike re-verify")
    lines.append("")
    lines.append("Old baseline (2026-07-03, pre-warmup-fix): `t_screen` cold **7.77s** / `t_find_divergences` cold **2.69s**.")
    lines.append("")
    lines.append("| Tool | Cold (this run) | Warm p95 (10 calls) | Notes |")
    lines.append("|---|---:|---:|---|")
    for r in mcp_results:
        cold_str = f"{r.cold_ms:.0f}ms" if r.cold_ms is not None else "ERR"
        warm_str = f"{r.warm_p95:.0f}ms" if r.warm_p95 is not None else "-"
        note = r.error or ""
        lines.append(f"| `{r.tool}` | {cold_str} | {warm_str} | {note} |")
    lines.append("")
    for r in mcp_results:
        if r.cold_ms is not None:
            old = 7770.0 if r.tool == "t_screen" else 2690.0
            lines.append(
                f"- `{r.tool}`: old cold baseline {old / 1000:.2f}s -> measured this run **{r.cold_ms / 1000:.2f}s** "
                f"({'CONFIRMS warmup fix' if r.cold_ms < old * 0.5 else 'does NOT clearly confirm the warmup fix'})."
            )
    lines.append("")

    lines.append("## Phase 4 — 5xx window scan")
    lines.append("")
    if not fivexx_windows:
        lines.append("No 5xx responses observed anywhere in the run.")
    else:
        lines.append("| Start (UTC) | End (UTC) | Duration | Endpoints hit | Count | Matches known export-swap pattern? |")
        lines.append("|---|---|---:|---|---:|---|")
        for w in fivexx_windows:
            start_str = time.strftime("%H:%M:%S", time.gmtime(w.start_wall))
            end_str = time.strftime("%H:%M:%S", time.gmtime(w.end_wall))
            matches_pattern = "yes" if w.duration_s <= 45 and len(w.endpoints) <= 2 else "NO — broader than the known pattern"
            lines.append(
                f"| {start_str} | {end_str} | {w.duration_s:.1f}s | {', '.join(w.endpoints)} | {w.count} | {matches_pattern} |"
            )
    lines.append("")

    lines.append("## Delivery-eve verdict")
    lines.append("")
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------


async def async_main(args: argparse.Namespace) -> dict[str, Any]:
    started_at = time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime())
    t_start = time.perf_counter()

    headers = {"User-Agent": "pytheum-stress-ramp/1.0"}
    async with httpx.AsyncClient(base_url=args.base_url, headers=headers) as client:
        ramp_results, aborted_whole_run, abort_reason = await phase1_rest_ramp(client)

        burst: BurstResult | None = None
        if not aborted_whole_run:
            burst = await phase2_limiter_burst(client)
        else:
            print(f"\nSKIPPING phase 2 (limiter burst) — whole-run abort: {abort_reason}", flush=True)
            burst = BurstResult(0, 0, 0, 0, 0, None, None, None)

    mcp_results: list[MCPToolResult] = []
    if not aborted_whole_run:
        mcp_results = phase3_mcp_coldspike(args.mcp_base)
    else:
        print(f"\nSKIPPING phase 3 (MCP cold-spike) — whole-run abort: {abort_reason}", flush=True)

    fivexx_windows = phase4_scan_5xx_windows(ALL_SAMPLES)

    duration_s = time.perf_counter() - t_start

    report_md = render_report(
        base_url=args.base_url,
        mcp_base=args.mcp_base,
        started_at=started_at,
        duration_s=duration_s,
        ramp_results=ramp_results,
        aborted_whole_run=aborted_whole_run,
        abort_reason=abort_reason,
        burst=burst,
        mcp_results=mcp_results,
        fivexx_windows=fivexx_windows,
    )

    out = {
        "base_url": args.base_url,
        "mcp_base": args.mcp_base,
        "started_at": started_at,
        "duration_s": duration_s,
        "aborted_whole_run": aborted_whole_run,
        "abort_reason": abort_reason,
        "report_markdown": report_md,
    }

    Path(args.out_md).write_text(report_md)
    print(f"\nReport written to {args.out_md}")

    if args.out_json:
        raw = {
            "ramp_results": [
                {
                    "name": r.name,
                    "path": r.path,
                    "aborted_after": r.aborted_after,
                    "levels": [lvl.__dict__ for lvl in r.levels],
                }
                for r in ramp_results
            ],
            "burst": burst.__dict__ if burst else None,
            "mcp_results": [r.__dict__ for r in mcp_results],
            "fivexx_windows": [w.__dict__ for w in fivexx_windows],
            "aborted_whole_run": aborted_whole_run,
            "abort_reason": abort_reason,
            "started_at": started_at,
            "duration_s": duration_s,
        }
        Path(args.out_json).write_text(json.dumps(raw, indent=2, default=str))
        print(f"Raw JSON written to {args.out_json}")

    print(f"Total runtime: {duration_s / 60:.1f} min ({duration_s:.0f}s)")
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--base-url", default=DEFAULT_BASE_URL)
    ap.add_argument("--mcp-base", default=DEFAULT_MCP_BASE)
    ap.add_argument("--out-md", default="docs/bench/2026-07-03-stress-ramp.md")
    ap.add_argument("--out-json", default="logs/stress_ramp_raw.json")
    args = ap.parse_args()
    asyncio.run(async_main(args))
    return 0


if __name__ == "__main__":
    sys.exit(main())
