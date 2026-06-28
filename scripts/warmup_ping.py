"""Keep the hosted serve instance warm — prevent scale-to-zero cold starts.

The FDE audit observed an ~11s first-request latency: a scaled-to-zero instance cold-boots
(container spin-up + the server start() sequence — trader clients, equivalence pre-warm,
curation load, DB pool) on the first request after an idle period; every subsequent request
is sub-second. A periodic keyless ``GET /v1/status`` keeps the instance warm so real
customer/agent traffic never pays that cold boot.

``/v1/status`` is the right target: keyless (never gated/rate-limited), cheap, and it touches
the equivalence index + the markets-count query, so a successful ping confirms the hot path
is warm — not just that the process is up.

Run modes:
  - one-shot (for an external cron / uptime monitor like cron-job.org, healthchecks.io):
      python -m scripts.warmup_ping
  - self-contained loop (a tiny always-on pinger; pair with launchd/systemd):
      python -m scripts.warmup_ping --loop --interval 240

The interval should be shorter than the host's scale-to-zero idle timeout (commonly 5–15 min
on free tiers); 240s (4 min) is a safe default. Base URL via --base or PYTHEUM_API_BASE.

Usage:
  python -m scripts.warmup_ping
  python -m scripts.warmup_ping --base https://api.pytheum.com --loop --interval 240
"""
from __future__ import annotations

import argparse
import json
import os
import time
import urllib.request

_DEFAULT_BASE = os.environ.get("PYTHEUM_API_BASE", "https://api.pytheum.com")
_SLOW_MS = 3000.0  # a ping over this almost certainly hit a cold boot


def ping(base: str, *, timeout: float = 30.0) -> tuple[bool, float, str]:
    """GET {base}/v1/status. Returns (ok, elapsed_ms, detail)."""
    url = base.rstrip("/") + "/v1/status"
    t0 = time.monotonic()
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "pytheum-warmup-ping"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = json.load(r)
        ms = (time.monotonic() - t0) * 1000.0
        pairs = (body.get("equivalence") or {}).get("pairs_loaded")
        return True, ms, f"pairs_loaded={pairs}"
    except Exception as e:  # noqa: BLE001 — a ping never raises; it reports
        ms = (time.monotonic() - t0) * 1000.0
        return False, ms, repr(e)[:120]


def _log(base: str) -> bool:
    ok, ms, detail = ping(base)
    flag = "OK " if ok else "FAIL"
    cold = "  <- COLD (slow)" if ok and ms > _SLOW_MS else ""
    stamp = time.strftime("%Y-%m-%dT%H:%M:%S")
    print(f"{stamp} warmup {flag} {ms:7.0f}ms  {detail}{cold}", flush=True)
    return ok


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--base", default=_DEFAULT_BASE, help="serve base URL")
    ap.add_argument("--loop", action="store_true", help="ping forever (self-contained pinger)")
    ap.add_argument("--interval", type=int, default=240, help="seconds between pings in --loop")
    args = ap.parse_args()

    if not args.loop:
        return 0 if _log(args.base) else 1
    print(f"warmup pinger: {args.base}/v1/status every {args.interval}s (Ctrl-C to stop)")
    while True:
        _log(args.base)
        time.sleep(args.interval)


if __name__ == "__main__":
    raise SystemExit(main())
