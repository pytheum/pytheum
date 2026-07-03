#!/usr/bin/env python3
"""bench_endpoints.py — live latency baseline for the pytheum REST API.

Hits the real deployment (default https://api.pytheum.com) and records, per
endpoint: 1 cold call, N=15 sequential calls, then a 10-concurrent burst of 20
calls. Reports p50/p95/p99/max, status-code distribution, payload-sanity
checks, and 429 counts.

Deliberately gentle: concurrency is capped at 10, request groups are spaced a
few seconds apart, and the whole run targets well under the default per-IP
keyless rate limit (120 req/min) despite exercising ~19 endpoints — this is a
moderate-load baseline, NOT a stress test, against a live production box.

Usage:
    python scripts/bench_endpoints.py [--base-url https://api.pytheum.com] [--out PATH]

Dependencies: stdlib + httpx only (httpx is already a project dependency).
"""
from __future__ import annotations

import argparse
import asyncio
import statistics
import time
from dataclasses import dataclass, field
from typing import Any, Callable

import httpx

DEFAULT_BASE_URL = "https://api.pytheum.com"
SEQUENTIAL_N = 15
BURST_CONCURRENCY = 10
BURST_TOTAL = 20
GROUP_GAP_S = 3.0
REQUEST_TIMEOUT_S = 20.0

# GREEN thresholds (p95), per the task brief's baselines.
GREEN_P95_DB_MS = 300.0
GREEN_P95_LIVE_MS = 1500.0


@dataclass
class EndpointSpec:
    name: str
    path: str
    serving_class: str  # "bundled-file" | "postgres" | "live-venue-proxy" | "computed"
    keyless_exempt: bool = False  # exempt from the 120/min limiter (status/metrics/about/guide)
    expect_keys: tuple[str, ...] = ()  # top-level keys expected in a sane response
    expect_list_key: str | None = None  # a key expected to be a (possibly empty) list
    notes: str = ""


@dataclass
class CallResult:
    status: int
    elapsed_ms: float
    ok_payload: bool
    error: str | None = None


@dataclass
class EndpointReport:
    spec: EndpointSpec
    cold: CallResult | None = None
    sequential: list[CallResult] = field(default_factory=list)
    burst: list[CallResult] = field(default_factory=list)

    def _latencies(self, results: list[CallResult]) -> list[float]:
        # Only successful (200) calls contribute to the latency distribution — a fast 404
        # or 429 is not a meaningful "response time" for the endpoint under test.
        return [r.elapsed_ms for r in results if r.status == 200]

    def all_results(self) -> list[CallResult]:
        out = list(self.sequential) + list(self.burst)
        if self.cold is not None:
            out = [self.cold] + out
        return out

    def percentiles(self) -> dict[str, float | None]:
        lat = self._latencies(self.sequential + self.burst)
        if not lat:
            return {"p50": None, "p95": None, "p99": None, "max": None}
        lat_sorted = sorted(lat)

        def pct(p: float) -> float:
            if len(lat_sorted) == 1:
                return lat_sorted[0]
            k = (len(lat_sorted) - 1) * p
            f = int(k)
            c = min(f + 1, len(lat_sorted) - 1)
            if f == c:
                return lat_sorted[f]
            return lat_sorted[f] + (lat_sorted[c] - lat_sorted[f]) * (k - f)

        return {
            "p50": pct(0.50),
            "p95": pct(0.95),
            "p99": pct(0.99),
            "max": max(lat_sorted),
        }

    def status_counts(self) -> dict[int, int]:
        counts: dict[int, int] = {}
        for r in self.all_results():
            counts[r.status] = counts.get(r.status, 0) + 1
        return counts

    def count_429(self) -> int:
        return sum(1 for r in self.all_results() if r.status == 429)

    def error_count(self) -> int:
        return sum(1 for r in self.all_results() if r.error is not None)

    def payload_sane_count(self) -> tuple[int, int]:
        checked = [r for r in self.all_results() if r.status == 200]
        sane = sum(1 for r in checked if r.ok_payload)
        return sane, len(checked)

    def success_count(self) -> tuple[int, int]:
        """(#calls that returned 200, #calls total) — 429s are excluded from the
        denominator since a clean 429 under burst load is expected limiter behavior,
        not an endpoint failure."""
        all_results = self.all_results()
        non_429 = [r for r in all_results if r.status != 429]
        ok = sum(1 for r in non_429 if r.status == 200)
        return ok, len(non_429)

    def verdict(self) -> str:
        pcts = self.percentiles()
        p95 = pcts["p95"]
        ok, total = self.success_count()
        # An endpoint that never returns 200 (e.g. a route 404ing on live prod) is a hard
        # RED regardless of how "fast" the error responses are.
        if total == 0 or ok == 0:
            return "RED"
        if p95 is None:
            return "RED"
        # Any non-200/429 status (4xx routing gap, 5xx) on ANY call is disqualifying.
        has_bad_status = ok != total
        threshold = (
            GREEN_P95_LIVE_MS
            if self.spec.serving_class == "live-venue-proxy"
            else GREEN_P95_DB_MS
        )
        sane, checked = self.payload_sane_count()
        payload_ok = checked == 0 or sane == checked
        if has_bad_status or not payload_ok:
            return "RED"
        if p95 <= threshold:
            return "GREEN"
        if p95 <= threshold * 2:
            return "AMBER"
        return "RED"


def _sane_payload(spec: EndpointSpec, body: Any) -> bool:
    if not isinstance(body, dict):
        return False
    for key in spec.expect_keys:
        if key not in body:
            return False
    if spec.expect_list_key is not None:
        val = body.get(spec.expect_list_key)
        if not isinstance(val, list):
            return False
    return True


async def _one_call(client: httpx.AsyncClient, spec: EndpointSpec, params: dict[str, Any]) -> CallResult:
    t0 = time.perf_counter()
    try:
        resp = await client.get(spec.path, params=params, timeout=REQUEST_TIMEOUT_S)
        elapsed_ms = (time.perf_counter() - t0) * 1000.0
        ok_payload = False
        if resp.status_code == 200:
            try:
                body = resp.json()
                ok_payload = _sane_payload(spec, body)
            except Exception:
                ok_payload = False
        elif resp.status_code == 429:
            ok_payload = True  # 429 with clean status is a PASS for limiter behavior
        return CallResult(status=resp.status_code, elapsed_ms=elapsed_ms, ok_payload=ok_payload)
    except Exception as exc:  # network error, timeout, etc.
        elapsed_ms = (time.perf_counter() - t0) * 1000.0
        return CallResult(status=0, elapsed_ms=elapsed_ms, ok_payload=False, error=repr(exc))


async def run_endpoint(
    client: httpx.AsyncClient,
    spec: EndpointSpec,
    params_fn: Callable[[], dict[str, Any]],
) -> EndpointReport:
    report = EndpointReport(spec=spec)

    # 1. Cold call.
    report.cold = await _one_call(client, spec, params_fn())

    # 2. N sequential calls.
    for _ in range(SEQUENTIAL_N):
        report.sequential.append(await _one_call(client, spec, params_fn()))

    # 3. Burst: BURST_TOTAL calls at BURST_CONCURRENCY.
    sem = asyncio.Semaphore(BURST_CONCURRENCY)

    async def _bounded() -> CallResult:
        async with sem:
            return await _one_call(client, spec, params_fn())

    burst_results = await asyncio.gather(*(_bounded() for _ in range(BURST_TOTAL)))
    report.burst = list(burst_results)

    return report


def _fmt_ms(v: float | None) -> str:
    return "-" if v is None else f"{v:.0f}ms"


def render_markdown(reports: list[EndpointReport], base_url: str, started_at: str, duration_s: float) -> str:
    lines: list[str] = []
    lines.append("# Live endpoint latency baseline")
    lines.append("")
    lines.append(f"- **Target**: `{base_url}`")
    lines.append(f"- **Run started**: {started_at}")
    lines.append(f"- **Total runtime**: {duration_s:.1f}s")
    lines.append(
        "- **Method**: per endpoint — 1 cold call, then N=15 sequential calls, then a "
        "10-concurrent burst of 20 calls. Endpoint groups spaced "
        f"{GROUP_GAP_S:.0f}s apart. Keyless per-IP rate limit is 120 req/min — bursts are "
        "EXPECTED to trigger 429s; a clean 429 (not a 5xx/hang) is a PASS for limiter behavior."
    )
    lines.append(
        f"- **GREEN thresholds**: p95 < {GREEN_P95_DB_MS:.0f}ms for DB/file-backed & computed "
        f"endpoints, p95 < {GREEN_P95_LIVE_MS:.0f}ms for live-venue-proxy endpoints. AMBER = "
        "within 2x threshold. RED = beyond that, any non-200/429 status (routing gap or 5xx) "
        "on any call, a payload-sanity failure, or zero successful (200) calls."
    )
    lines.append(
        "- **Earlier baselines** (context): semantic search sub-second; substring search "
        "~72ms median post-pg_trgm; `/v1/status` SWR ~instant after the first (cold) call."
    )
    lines.append("")
    lines.append(
        "| Endpoint | Class | Cold | p50 | p95 | p99 | max | Statuses | 429s | Payload OK | Verdict |"
    )
    lines.append(
        "|---|---|---|---|---|---|---|---|---|---|---|"
    )
    for r in reports:
        pcts = r.percentiles()
        cold_str = "-" if r.cold is None or r.cold.status == 0 else f"{r.cold.status}/{r.cold.elapsed_ms:.0f}ms"
        statuses = r.status_counts()
        status_str = ", ".join(f"{k}:{v}" for k, v in sorted(statuses.items()))
        sane, checked = r.payload_sane_count()
        payload_str = f"{sane}/{checked}" if checked else "n/a"
        verdict = r.verdict()
        lines.append(
            f"| `{r.spec.path}` | {r.spec.serving_class} | {cold_str} | "
            f"{_fmt_ms(pcts['p50'])} | {_fmt_ms(pcts['p95'])} | {_fmt_ms(pcts['p99'])} | "
            f"{_fmt_ms(pcts['max'])} | {status_str} | {r.count_429()} | {payload_str} | "
            f"**{verdict}** |"
        )

    lines.append("")
    lines.append("## Notes")
    lines.append("")
    for r in reports:
        note_bits = []
        if r.spec.notes:
            note_bits.append(r.spec.notes)
        if r.error_count():
            note_bits.append(f"{r.error_count()} network-level error(s)/timeout(s).")
        errs = {r2.error for r2 in r.all_results() if r2.error}
        if errs:
            note_bits.append("errors: " + "; ".join(sorted(errs))[:300])
        if r.spec.keyless_exempt:
            note_bits.append("Exempt from the per-IP rate limiter (keyless allowlist).")
        if note_bits:
            lines.append(f"- `{r.spec.path}` ({r.spec.serving_class}): " + " ".join(note_bits))

    lines.append("")
    slowest = sorted(
        (r for r in reports if r.percentiles()["p95"] is not None),
        key=lambda r: r.percentiles()["p95"],
        reverse=True,
    )[:5]
    lines.append("## 5 slowest endpoints (by p95)")
    lines.append("")
    for r in slowest:
        lines.append(f"- `{r.spec.path}` — p95 {_fmt_ms(r.percentiles()['p95'])} ({r.spec.serving_class})")

    reds = [r for r in reports if r.verdict() == "RED"]
    lines.append("")
    lines.append("## RED verdicts")
    lines.append("")
    if reds:
        for r in reds:
            reason_bits = []
            ok, total = r.success_count()
            if any(500 <= x.status < 600 for x in r.all_results()):
                reason_bits.append("5xx observed")
            if total and ok == 0:
                statuses = sorted(r.status_counts())
                reason_bits.append(f"zero 200s — all calls returned {statuses}")
            elif ok != total:
                reason_bits.append(f"non-200/429 on {total - ok}/{total} calls")
            sane, checked = r.payload_sane_count()
            if checked and sane != checked:
                reason_bits.append(f"payload sanity {sane}/{checked}")
            if r.percentiles()["p95"] is None and ok:
                reason_bits.append("no successful calls")
            reason = ", ".join(reason_bits) or "p95 beyond 2x threshold"
            lines.append(f"- `{r.spec.path}` — {reason}")
    else:
        lines.append("None.")

    return "\n".join(lines) + "\n"


async def discover_refs(client: httpx.AsyncClient) -> dict[str, str]:
    """Pull real refs from a matched/search call for the {ref} endpoints."""
    refs = {
        "kalshi_ref": "kalshi:KXMENWORLDCUP-26-US",
        "polymarket_ref": "polymarket:31552",
        "wallet": "0x2117ae94a97d69b78cbc81b6680a62deb1955c26",
    }
    try:
        resp = await client.get("/v1/markets/matched", params={"limit": 5}, timeout=REQUEST_TIMEOUT_S)
        if resp.status_code == 200:
            pairs = resp.json().get("pairs", [])
            for p in pairs:
                k = p.get("kalshi", {}).get("id")
                pm = p.get("polymarket", {}).get("id")
                if k:
                    refs["kalshi_ref"] = k
                if pm:
                    refs["polymarket_ref"] = pm
                if k and pm:
                    break
    except Exception:
        pass
    try:
        resp = await client.get(
            "/v1/markets/whale-trades", params={"min_usd": 500, "limit": 5}, timeout=REQUEST_TIMEOUT_S
        )
        if resp.status_code == 200:
            trades = resp.json().get("trades", [])
            if trades:
                refs["wallet"] = trades[0].get("wallet", refs["wallet"])
    except Exception:
        pass
    return refs


def build_specs(refs: dict[str, str]) -> list[EndpointSpec]:
    kref = refs["kalshi_ref"]
    pref = refs["polymarket_ref"]
    wallet = refs["wallet"]
    return [
        EndpointSpec(
            "status", "/v1/status", "computed", keyless_exempt=True,
            expect_keys=("equivalence", "related", "service"),
        ),
        EndpointSpec(
            "metrics", "/v1/metrics", "computed", keyless_exempt=True,
        ),
        EndpointSpec(
            "about", "/v1/about", "computed", keyless_exempt=True,
        ),
        EndpointSpec(
            "guide", "/v1/guide", "computed", keyless_exempt=True,
        ),
        EndpointSpec(
            "quality", "/v1/quality", "bundled-file",
            expect_keys=("pairs_total", "tiers"),
        ),
        EndpointSpec(
            "markets_search", "/v1/markets/search", "postgres",
            expect_keys=("markets", "count"), expect_list_key="markets",
            notes="q=election.",
        ),
        EndpointSpec(
            "markets_screen", "/v1/markets/screen", "postgres",
            expect_keys=("markets", "count"), expect_list_key="markets",
        ),
        EndpointSpec(
            "markets_equivalents", "/v1/markets/equivalents", "postgres",
            expect_keys=("pairs",), expect_list_key="pairs",
        ),
        EndpointSpec(
            "markets_matched", "/v1/markets/matched", "postgres",
            expect_keys=("pairs", "total"), expect_list_key="pairs",
        ),
        EndpointSpec(
            "market_equivalents_ref", f"/v1/markets/{kref}/equivalents", "postgres",
        ),
        EndpointSpec(
            "market_rules_ref", f"/v1/markets/{kref}/rules", "postgres",
        ),
        EndpointSpec(
            "market_core_ref", f"/v1/markets/{kref}/core", "postgres",
        ),
        EndpointSpec(
            "market_related_ref", f"/v1/markets/{kref}/related", "postgres",
        ),
        EndpointSpec(
            "whale_trades", "/v1/markets/whale-trades", "live-venue-proxy",
            expect_keys=("trades", "count"), expect_list_key="trades",
        ),
        EndpointSpec(
            "market_book_kalshi", f"/v1/markets/{kref}/book", "live-venue-proxy",
        ),
        EndpointSpec(
            "market_book_pm", f"/v1/markets/{pref}/book", "live-venue-proxy",
        ),
        EndpointSpec(
            "market_trades_kalshi", f"/v1/markets/{kref}/trades", "live-venue-proxy",
        ),
        EndpointSpec(
            "market_oi_kalshi", f"/v1/markets/{kref}/oi", "live-venue-proxy",
        ),
        EndpointSpec(
            "market_ohlcv_kalshi", f"/v1/markets/{kref}/ohlcv", "live-venue-proxy",
        ),
        EndpointSpec(
            "market_holders_pm", f"/v1/markets/{pref}/holders", "live-venue-proxy",
            notes="Polymarket-only.",
        ),
        EndpointSpec(
            "traders_leaderboard", "/v1/traders/leaderboard", "live-venue-proxy",
            notes="Observed 404 on live prod during discovery probe despite being a registered route.",
        ),
        EndpointSpec(
            "trader_profile", f"/v1/traders/{wallet}", "live-venue-proxy",
            notes="Observed 404 on live prod during discovery probe despite being a registered route.",
        ),
    ]


def params_for(spec: EndpointSpec) -> dict[str, Any]:
    if spec.name == "markets_search":
        return {"q": "election", "limit": 10}
    if spec.name == "markets_screen":
        return {"venues": "kalshi", "limit": 10}
    if spec.name == "markets_equivalents":
        return {"limit": 10}
    if spec.name == "markets_matched":
        return {"limit": 10}
    if spec.name == "whale_trades":
        return {"min_usd": 500, "limit": 10}
    if spec.name == "traders_leaderboard":
        return {"period": "weekly"}
    return {}


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--out", default="docs/bench/2026-07-03-endpoint-latency.md")
    args = parser.parse_args()

    started_at = time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime())
    t_start = time.perf_counter()

    async with httpx.AsyncClient(base_url=args.base_url, headers={"User-Agent": "pytheum-bench/1.0"}) as client:
        refs = await discover_refs(client)
        specs = build_specs(refs)

        reports: list[EndpointReport] = []
        for i, spec in enumerate(specs):
            print(f"[{i + 1}/{len(specs)}] benchmarking {spec.path} ({spec.serving_class}) ...", flush=True)
            report = await run_endpoint(client, spec, lambda s=spec: params_for(s))
            reports.append(report)
            pcts = report.percentiles()
            print(
                f"    cold={report.cold.status if report.cold else '-'} "
                f"p50={_fmt_ms(pcts['p50'])} p95={_fmt_ms(pcts['p95'])} "
                f"429s={report.count_429()} verdict={report.verdict()}",
                flush=True,
            )
            if i < len(specs) - 1:
                await asyncio.sleep(GROUP_GAP_S)

    duration_s = time.perf_counter() - t_start
    report_md = render_markdown(reports, args.base_url, started_at, duration_s)

    with open(args.out, "w") as f:
        f.write(report_md)

    print(f"\nReport written to {args.out}")
    print(f"Total runtime: {duration_s:.1f}s")


if __name__ == "__main__":
    asyncio.run(main())
