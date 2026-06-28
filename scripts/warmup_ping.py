"""Keep the hosted serve instance's HOT WORKING SET warm — prevent cold-buffer spikes.

The instance is a persistent, always-on process (NOT scale-to-zero — verified on-box 2026-06-28),
so the ~6–11s spikes on the first request after an idle stretch are NOT a container cold-boot.
They are **cold DB buffers**: on a memory-pressured box, Postgres evicts the `markets` table and
its trigram/volume search indexes from `shared_buffers` while idle, so the next substring search
re-reads them from disk. A periodic keyless touch of the heavy read path keeps that set resident.

Targets (all keyless — never gated/rate-limited):
  - ``GET /v1/status``            — liveness + equivalence index + markets-count (process warm).
  - ``GET /v1/markets/search?q=`` — a few common terms, to keep the `markets` table + the
                                    trigram/volume search indexes + the high-volume heap pages hot
                                    in `shared_buffers`. **This is the path that actually
                                    cold-spikes — `/v1/status` alone does NOT warm it.** Terms via
                                    ``PYTHEUM_WARMUP_QUERIES`` (comma-separated; the defaults span
                                    the highest-volume verticals so their hot heap/index pages,
                                    which most queries share, stay resident).

The response cache (30s TTL) is shorter than the ping interval, so each warm search is a real
cache-miss that re-touches the DB — which is the point (we want it to warm buffers, not serve a
cached body). Deeper, durable fix for the long tail is right-sizing the box (more RAM); this
keep-warm covers the hot path between requests.

Run modes:
  - one-shot (for an external cron / uptime monitor):
      python -m scripts.warmup_ping
  - self-contained loop (a tiny always-on pinger; pair with launchd/systemd):
      python -m scripts.warmup_ping --loop --interval 240

240s (4 min) is a safe default. Base URL via --base or PYTHEUM_API_BASE.
"""
from __future__ import annotations

import argparse
import json
import os
import time
import urllib.request
from urllib.parse import quote

_DEFAULT_BASE = os.environ.get("PYTHEUM_API_BASE", "https://api.pytheum.com")
_SLOW_MS = 3000.0  # a ping over this almost certainly hit cold buffers
# Highest-volume verticals: their hot heap + shared index pages overlap with most real queries,
# so touching a few keeps the common working set warm without hammering the box.
_DEFAULT_QUERIES = "bitcoin,ethereum,trump,election"


def warmup_targets() -> list[str]:
    """Keyless warm paths: status (liveness/process) + a few searches (the cold-buffer path)."""
    queries = [q.strip() for q in os.environ.get("PYTHEUM_WARMUP_QUERIES", _DEFAULT_QUERIES).split(",") if q.strip()]
    return ["/v1/status", *[f"/v1/markets/search?q={quote(q)}&limit=50" for q in queries]]


def ping(base: str, path: str, *, timeout: float = 30.0) -> tuple[bool, float, str]:
    """GET {base}{path}. Returns (ok, elapsed_ms, detail). Never raises — a ping reports."""
    url = base.rstrip("/") + path
    t0 = time.monotonic()
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "pytheum-warmup-ping"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = json.load(r)
        ms = (time.monotonic() - t0) * 1000.0
        detail = ""
        if isinstance(body, dict):
            if "equivalence" in body:  # /v1/status
                detail = f"pairs_loaded={(body.get('equivalence') or {}).get('pairs_loaded')}"
            elif "count" in body:  # /v1/markets/search
                detail = f"count={body.get('count')}"
        return True, ms, detail
    except Exception as e:  # noqa: BLE001 — a ping never raises; it reports
        ms = (time.monotonic() - t0) * 1000.0
        return False, ms, repr(e)[:120]


def _log(base: str) -> bool:
    """Warm every target once; return True iff all succeeded."""
    all_ok = True
    for path in warmup_targets():
        ok, ms, detail = ping(base, path)
        all_ok = all_ok and ok
        flag = "OK " if ok else "FAIL"
        cold = "  <- COLD (slow)" if ok and ms > _SLOW_MS else ""
        stamp = time.strftime("%Y-%m-%dT%H:%M:%S")
        print(f"{stamp} warmup {flag} {ms:7.0f}ms  {path:34} {detail}{cold}", flush=True)
    return all_ok


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--base", default=_DEFAULT_BASE, help="serve base URL")
    ap.add_argument("--loop", action="store_true", help="ping forever (self-contained pinger)")
    ap.add_argument("--interval", type=int, default=240, help="seconds between pings in --loop")
    args = ap.parse_args()

    if not args.loop:
        return 0 if _log(args.base) else 1
    n = len(warmup_targets())
    print(f"warmup pinger: {n} keyless targets on {args.base} every {args.interval}s (Ctrl-C to stop)")
    while True:
        _log(args.base)
        time.sleep(args.interval)


if __name__ == "__main__":
    raise SystemExit(main())
