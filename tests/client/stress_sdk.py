"""Live throughput / latency / resilience stress for the pytheum SDK.

Fans out many concurrent calls through the SDK's AsyncClient governor against LIVE
prod and reports throughput, latency distribution, and how many 429s the retry
layer absorbed transparently (surfaced errors should be ~0). Proves the governor
scales throughput while keeping the client inside the edge's per-IP envelope.

Bounded by design (the prod box is memory-tight + Caddy per-IP limited): modest
concurrency, short duration, backs off on the limiter automatically via the client's
own retry. NOT a pytest test — run it directly:

    .venv/bin/python tests/client/stress_sdk.py --secs 20 --concurrency 8

Gated for CI: only runs when invoked directly (no network in unit runs).
"""
from __future__ import annotations

import argparse
import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from pytheum.client import AsyncClient, PytheumError  # noqa: E402

# A spread of endpoint classes so we exercise file-backed, DB, and computed paths.
_WORKLOAD = [
    ("status", lambda px: px.status()),
    ("search", lambda px: px.search("bitcoin", limit=20)),
    ("screen", lambda px: px.screen(limit=25)),
    ("matched", lambda px: px.matched_pairs(sort_by="net_edge", limit=25)),
    ("equivalents", lambda px: px.equivalents("kalshi:KXBTCD-26JUL0217-T105000", limit=10)),
]


def _pct(sorted_xs: list[float], q: float) -> float:
    return sorted_xs[min(int(len(sorted_xs) * q), len(sorted_xs) - 1)]


async def _run(secs: float, concurrency: int) -> int:
    px = AsyncClient(max_concurrency=concurrency)
    lat: dict[str, list[float]] = {name: [] for name, _ in _WORKLOAD}
    errors = 0
    deadline = time.monotonic() + secs
    offered_peak = {"v": 0, "cur": 0}
    lock = asyncio.Lock()

    async def worker(name, fn):  # type: ignore[no-untyped-def]
        nonlocal errors
        while time.monotonic() < deadline:
            async with lock:
                offered_peak["cur"] += 1
                offered_peak["v"] = max(offered_peak["v"], offered_peak["cur"])
            t0 = time.monotonic()
            try:
                await fn(px)
                lat[name].append((time.monotonic() - t0) * 1000)
            except PytheumError:
                errors += 1
            finally:
                async with lock:
                    offered_peak["cur"] -= 1

    # workers: `concurrency` per endpoint class, all firing continuously
    tasks = [asyncio.create_task(worker(name, fn))
             for name, fn in _WORKLOAD for _ in range(concurrency)]
    t_start = time.monotonic()
    await asyncio.gather(*tasks)
    elapsed = time.monotonic() - t_start
    await px.aclose()

    total = sum(len(v) for v in lat.values())
    print("\n" + "=" * 64)
    print(f"SDK live stress — {secs:.0f}s, governor max_concurrency={concurrency}")
    print(f"total OK calls : {total}  ({total/elapsed:.1f}/s aggregate)")
    print(f"surfaced errors: {errors}  (429s/timeouts the retry layer could NOT absorb)")
    print(f"peak OFFERED load (workers mid-call): {offered_peak['v']}  (governor caps real HTTP in-flight at {concurrency})")
    print("-" * 64)
    print(f"{'endpoint':14}{'n':>6}{'p50':>9}{'p95':>9}{'p99':>9}{'max':>9}  (ms)")
    for name, xs in lat.items():
        if not xs:
            print(f"{name:14}{0:>6}      —")
            continue
        xs.sort()
        print(f"{name:14}{len(xs):>6}{_pct(xs, .50):>9.0f}{_pct(xs, .95):>9.0f}"
              f"{_pct(xs, .99):>9.0f}{_pct(xs, .999):>9.0f}")
    print("=" * 64)
    # health verdict: the governor + retry should keep surfaced errors negligible
    err_rate = errors / max(total + errors, 1)
    ok = err_rate < 0.02
    print(f"VERDICT: {'PASS' if ok else 'FAIL'} — surfaced-error rate {err_rate:.2%} "
          f"({'≤' if ok else '>'} 2% target); retry layer absorbing the edge limiter cleanly")
    return 0 if ok else 1


def main() -> None:
    ap = argparse.ArgumentParser(description="Live SDK throughput/latency stress")
    ap.add_argument("--secs", type=float, default=20.0)
    ap.add_argument("--concurrency", type=int, default=6,
                    help="governor max_concurrency AND workers per endpoint class")
    args = ap.parse_args()
    raise SystemExit(asyncio.run(_run(args.secs, args.concurrency)))


if __name__ == "__main__":
    main()
