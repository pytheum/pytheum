#!/usr/bin/env python3
"""Latency baseline for the LIVE pytheum MCP server (streamable-http, mcp.pytheum.com).

Enumerates every t_* tool via tools/list, builds a minimal valid call per tool
(discovering real market/wallet refs from t_matched_pairs / t_search_markets /
t_leaderboard first, mirroring the exploratory run_all.py driver), then runs
1 cold call + 10 sequential timed calls per tool. Emits a JSON report to stdout
(or --out) consumed by the doc generator / used directly for
docs/bench/2026-07-03-mcp-latency.md.

Protocol handling (initialize -> Mcp-Session-Id header -> tools/call, SSE
`data:` framing) is lifted from the scratchpad reference driver `drive.py` —
a minimal stdlib urllib JSON-RPC/SSE client speaking the same wire format
FastMCP's streamable-http transport emits.

Usage:
    python3 scripts/bench_mcp.py [--base URL] [--calls N] [--sleep SECS] [--out FILE]
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
import urllib.error
import urllib.request
from typing import Any

DEFAULT_BASE = "https://mcp.pytheum.com/"
HDR = {
    "Content-Type": "application/json",
    "Accept": "application/json, text/event-stream",
}


class RateLimited(Exception):
    """Raised when the server's per-IP token-bucket limiter returns 429."""


class MCPClient:
    """Minimal JSON-RPC/SSE driver for FastMCP streamable-http — protocol
    lifted from the scratchpad reference `drive.py` (initialize -> session id
    header -> tools/call, parsing the SSE `data:` framing FastMCP emits)."""

    def __init__(self, base: str = DEFAULT_BASE, timeout: float = 40.0):
        self.base = base
        self.timeout = timeout
        self.session_id: str | None = None
        self._id = 10

    def _raw(self, payload: dict[str, Any], want_headers: bool = False):
        data = json.dumps(payload).encode()
        headers = dict(HDR)
        if self.session_id:
            headers["Mcp-Session-Id"] = self.session_id
        req = urllib.request.Request(self.base, data=data, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                body = r.read().decode()
                hdrs = dict(r.headers)
        except urllib.error.HTTPError as e:
            if e.code == 429:
                raise RateLimited(f"429 rate_limited: {e.read().decode(errors='replace')[:200]}")
            body = e.read().decode(errors="replace")
            hdrs = dict(e.headers or {})
            out = None
            for ln in body.splitlines():
                if ln.startswith("data:"):
                    try:
                        out = json.loads(ln[5:].strip())
                    except Exception:
                        pass
                    break
            if out is None:
                out = {"error": {"code": e.code, "message": body[:500]}}
            if want_headers:
                return out, hdrs
            return out
        out = None
        for ln in body.splitlines():
            if ln.startswith("data:"):
                out = json.loads(ln[5:].strip())
                break
        if out is None and body.strip().startswith("{"):
            out = json.loads(body)
        if want_headers:
            return out, hdrs
        return out

    def initialize(self) -> None:
        _, hdrs = self._raw(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {"name": "bench_mcp", "version": "1"},
                },
            },
            want_headers=True,
        )
        self.session_id = hdrs.get("Mcp-Session-Id") or hdrs.get("mcp-session-id")
        self._raw({"jsonrpc": "2.0", "method": "notifications/initialized"})

    def list_tools(self) -> list[dict[str, Any]]:
        self._id += 1
        r = self._raw({"jsonrpc": "2.0", "id": self._id, "method": "tools/list", "params": {}})
        if not r or "result" not in r:
            raise RuntimeError(f"tools/list failed: {r}")
        return r["result"].get("tools", [])

    def call(self, tool: str, args: dict[str, Any] | None = None) -> dict[str, Any]:
        self._id += 1
        r = self._raw(
            {
                "jsonrpc": "2.0",
                "id": self._id,
                "method": "tools/call",
                "params": {"name": tool, "arguments": args or {}},
            }
        )
        if r is None:
            return {"_error": "no_response"}
        if "error" in r:
            return {"_error": r["error"]}
        content = r.get("result", {}).get("content", [])
        for c in content:
            if c.get("type") == "text":
                try:
                    return json.loads(c["text"])
                except Exception:
                    return {"_raw_text": c["text"]}
        return r.get("result", {})


def _d(res: Any) -> Any:
    return res.get("data") if isinstance(res, dict) else res


# --------------------------------------------------------------------------
# Upstream-class map: which REST route (and which class of backend) each
# t_* tool wraps, inferred from src/pytheum/mcp/tools.py.
#   bundled  - local file read (equivalence/related JSONL), keyless, no venue hop
#   db       - pytheum's own market-store-backed API route
#   semantic - embedding/similarity search
#   live-venue - direct Kalshi/Polymarket API fetch (coalesced + short-TTL cached)
#   computed - aggregates multiple routes + does math server-side
# --------------------------------------------------------------------------
TOOL_CLASS: dict[str, str] = {
    "t_status": "bundled",
    "t_quality": "bundled",
    "t_guide": "bundled",
    "t_about": "bundled",
    "t_matched_pairs": "bundled",
    "t_equivalent_markets": "bundled",
    "t_market_rules": "bundled",
    "t_related_markets": "bundled",
    "t_get_market": "db",
    "t_market_context": "db",
    "t_bundle_context": "db",
    "t_market_history": "db",
    "t_context_batch": "db",
    "t_screen": "db",
    "t_search_markets": "db",
    "t_find_markets": "semantic",
    "t_event_related_markets": "semantic",
    "t_find_divergences": "computed",
    "t_orderbook": "live-venue",
    "t_recent_trades": "live-venue",
    "t_open_interest": "live-venue",
    "t_leaderboard": "live-venue",
    "t_trader_profile": "live-venue",
    "t_market_holders": "live-venue",
    "t_whale_trades": "live-venue",
    "t_ohlcv": "live-venue",
    "t_market_flow": "live-venue",
}

TOOL_ROUTE: dict[str, str] = {
    "t_status": "GET /v1/status",
    "t_quality": "GET /v1/quality",
    "t_guide": "(local, no network)",
    "t_about": "(local, no network)",
    "t_matched_pairs": "GET /v1/markets/matched",
    "t_equivalent_markets": "GET /v1/markets/{ref}/equivalents",
    "t_market_rules": "GET /v1/markets/{ref}/rules",
    "t_related_markets": "GET /v1/markets/{ref}/related",
    "t_get_market": "GET /v1/markets/{ref}/core",
    "t_market_context": "GET /v1/markets/{ref}/context",
    "t_bundle_context": "GET /v1/bundles/{ref}/context",
    "t_market_history": "GET /v1/markets/{ref}/history",
    "t_context_batch": "GET /v1/markets/{ref}/context xN (fan-out, capped concurrency)",
    "t_screen": "GET /v1/markets/screen",
    "t_search_markets": "GET /v1/markets/search",
    "t_find_markets": "GET /v1/markets/relevant-to",
    "t_event_related_markets": "GET /v1/events/{id}/related-markets",
    "t_find_divergences": "GET /v1/markets/equivalents xN (multi-fetch + fee/edge math)",
    "t_orderbook": "GET /v1/markets/{ref}/book",
    "t_recent_trades": "GET /v1/markets/{ref}/trades",
    "t_open_interest": "GET /v1/markets/{ref}/oi",
    "t_leaderboard": "GET /v1/traders/leaderboard",
    "t_trader_profile": "GET /v1/traders/{wallet}",
    "t_market_holders": "GET /v1/markets/{ref}/holders",
    "t_whale_trades": "GET /v1/markets/whale-trades",
    "t_ohlcv": "GET /v1/markets/{ref}/ohlcv",
    "t_market_flow": "GET /v1/markets/{ref}/flow",
}


def discover_refs(client: MCPClient) -> dict[str, Any]:
    """Pull real market_ref / wallet / bundle_ref / event_id seeds from live
    data, mirroring the scratchpad's run_all.py discovery steps."""
    refs: dict[str, Any] = {"k_ref": None, "p_ref": None, "wallet": None, "bundle_ref": None}

    mp = _d(client.call("t_matched_pairs", {"limit": 8, "sort_by": "net_edge"}))
    pairs = (mp or {}).get("pairs", []) if isinstance(mp, dict) else []
    for pr in pairs:
        if not refs["k_ref"] and pr.get("kalshi", {}).get("id"):
            refs["k_ref"] = pr["kalshi"]["id"]
        if not refs["p_ref"] and pr.get("polymarket", {}).get("id"):
            refs["p_ref"] = pr["polymarket"]["id"]

    if not refs["k_ref"] or not refs["p_ref"]:
        sm = _d(client.call("t_search_markets", {"q": "bitcoin", "limit": 5}))
        rows = (sm or {}).get("markets", []) if isinstance(sm, dict) else []
        for r in rows:
            vid = r.get("id") or ""
            if not refs["k_ref"] and vid.startswith("kalshi:"):
                refs["k_ref"] = vid
            if not refs["p_ref"] and vid.startswith("polymarket:"):
                refs["p_ref"] = vid

    lb = _d(client.call("t_leaderboard", {"period": "weekly"}))
    if isinstance(lb, dict):
        for key in ("leaders", "traders", "leaderboard", "rows"):
            rows = lb.get(key)
            if isinstance(rows, list) and rows:
                refs["wallet"] = rows[0].get("wallet") or rows[0].get("address") or rows[0].get("proxyWallet")
                break

    if not refs["wallet"]:
        wt = _d(client.call("t_whale_trades", {"min_usd": 10000, "limit": 3}))
        rows = wt.get("trades") if isinstance(wt, dict) else wt
        if isinstance(rows, list) and rows:
            refs["wallet"] = rows[0].get("wallet") or rows[0].get("proxyWallet")

    if refs["k_ref"]:
        refs["bundle_ref"] = refs["k_ref"].rsplit("-", 1)[0]

    return refs


def build_call_args(tool: str, refs: dict[str, Any]) -> dict[str, Any]:
    """One valid minimal-args call per tool, args discovered from live refs."""
    k_ref, p_ref = refs.get("k_ref"), refs.get("p_ref")
    wallet = refs.get("wallet")
    bundle_ref = refs.get("bundle_ref")

    table: dict[str, dict[str, Any]] = {
        "t_status": {},
        "t_quality": {},
        "t_guide": {},
        "t_about": {},
        "t_find_markets": {"query": "Fed cuts interest rates in 2026", "limit": 3},
        "t_search_markets": {"q": "bitcoin", "limit": 3},
        "t_screen": {"sort_by": "move", "limit": 3},
        "t_matched_pairs": {"limit": 5, "sort_by": "net_edge"},
        "t_equivalent_markets": {"market_ref": k_ref},
        "t_find_divergences": {"min_net_edge": 0.0, "limit": 3},
        "t_related_markets": {"market_ref": k_ref},
        "t_get_market": {"market_ref": k_ref},
        "t_market_context": {"market_ref": k_ref, "limit": 2},
        "t_market_rules": {"market_ref": k_ref},
        "t_market_history": {"market_ref": k_ref, "limit": 3},
        "t_ohlcv": {"market_ref": k_ref, "limit": 3},
        "t_context_batch": {"market_refs": [r for r in (k_ref, p_ref) if r], "limit": 1},
        "t_orderbook": {"market_ref": k_ref, "depth": 3},
        "t_recent_trades": {"market_ref": k_ref, "limit": 3},
        "t_open_interest": {"market_ref": k_ref},
        "t_market_flow": {"market_ref": p_ref, "window_hours": 24},
        "t_market_holders": {"market_ref": p_ref},
        "t_whale_trades": {"min_usd": 10000, "limit": 3},
        "t_leaderboard": {"period": "weekly"},
        "t_trader_profile": {"wallet": wallet} if wallet else {},
        "t_bundle_context": {"bundle_ref": bundle_ref, "limit": 2},
        "t_event_related_markets": {"event_id": "test", "limit": 2},
    }
    return table.get(tool, {})


def payload_sane(tool: str, res: Any) -> tuple[bool, str]:
    """Loose sanity check: ok envelope true (or an explanatory {error,hint}
    degraded response, which is a valid documented shape for several tools),
    and a data/error payload actually present."""
    if isinstance(res, dict) and "_error" in res:
        return False, f"transport/JSON-RPC error: {str(res['_error'])[:150]}"
    if not isinstance(res, dict):
        return False, f"non-dict payload: {type(res).__name__}"
    if res.get("ok") is False:
        # A structured {ok:false, error, hint} is a documented degraded path,
        # not a broken tool — flag it as such rather than a hard failure.
        return True, f"ok=false (documented error path): {res.get('error')}"
    if "ok" not in res:
        return False, "missing 'ok' envelope key"
    data = res.get("data")
    if data is None:
        return False, "ok=true but data is null"
    return True, "ok"


def bench_tool(client: MCPClient, tool: str, args: dict[str, Any], n_calls: int, sleep_s: float) -> dict[str, Any]:
    result: dict[str, Any] = {
        "tool": tool,
        "args": args,
        "class": TOOL_CLASS.get(tool, "unknown"),
        "route": TOOL_ROUTE.get(tool, "unknown"),
        "cold_ms": None,
        "sequential_ms": [],
        "errors": [],
        "rate_limited": False,
        "sane": None,
        "sane_note": "",
    }

    # cold call
    try:
        t0 = time.perf_counter()
        res = client.call(tool, args)
        cold_ms = (time.perf_counter() - t0) * 1000
        result["cold_ms"] = round(cold_ms, 1)
        ok, note = payload_sane(tool, res)
        result["sane"], result["sane_note"] = ok, note
        if not ok:
            result["errors"].append(f"cold: {note}")
    except RateLimited as e:
        result["rate_limited"] = True
        result["errors"].append(f"cold: {e}")
        return result
    except Exception as e:
        result["errors"].append(f"cold: {type(e).__name__}: {e}")
        return result

    if sleep_s:
        time.sleep(sleep_s)

    for i in range(n_calls):
        try:
            t0 = time.perf_counter()
            res = client.call(tool, args)
            elapsed = (time.perf_counter() - t0) * 1000
            result["sequential_ms"].append(round(elapsed, 1))
            ok, note = payload_sane(tool, res)
            if not ok:
                result["errors"].append(f"call {i}: {note}")
        except RateLimited as e:
            result["rate_limited"] = True
            result["errors"].append(f"call {i}: {e}")
            break
        except Exception as e:
            result["errors"].append(f"call {i}: {type(e).__name__}: {e}")
        if sleep_s:
            time.sleep(sleep_s)

    return result


def percentile(values: list[float], pct: float) -> float | None:
    if not values:
        return None
    s = sorted(values)
    k = (len(s) - 1) * pct
    f, c = int(k), min(int(k) + 1, len(s) - 1)
    if f == c:
        return s[f]
    return s[f] + (s[c] - s[f]) * (k - f)


def summarize(result: dict[str, Any]) -> dict[str, Any]:
    seq = result["sequential_ms"]
    p50 = percentile(seq, 0.50)
    p95 = percentile(seq, 0.95)
    cls = result["class"]
    threshold = 1000.0 if cls in ("db", "bundled") else 2500.0
    verdict = "RED"
    if result["rate_limited"]:
        verdict = "RATE_LIMITED"
    elif p95 is not None and not result["errors"]:
        verdict = "GREEN" if p95 < threshold else "RED"
    elif p95 is not None and result["errors"]:
        verdict = "RED"
    return {
        **result,
        "p50_ms": round(p50, 1) if p50 is not None else None,
        "p95_ms": round(p95, 1) if p95 is not None else None,
        "n_ok": len(seq),
        "threshold_ms": threshold,
        "verdict": verdict,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--base", default=DEFAULT_BASE)
    ap.add_argument("--calls", type=int, default=10, help="sequential timed calls per tool (after 1 cold call)")
    ap.add_argument("--sleep", type=float, default=0.3, help="seconds between calls (politeness re: rate limiter)")
    ap.add_argument("--out", default=None, help="write JSON report here (default: stdout)")
    args = ap.parse_args()

    client = MCPClient(base=args.base)
    print(f"# connecting to {args.base} ...", file=sys.stderr)
    client.initialize()
    print(f"# session={client.session_id}", file=sys.stderr)

    tools = client.list_tools()
    tool_names = sorted(t["name"] for t in tools if t.get("name", "").startswith("t_"))
    print(f"# discovered {len(tool_names)} tools: {tool_names}", file=sys.stderr)

    print("# discovering live refs (t_matched_pairs / t_search_markets / t_leaderboard) ...", file=sys.stderr)
    refs = discover_refs(client)
    print(f"# refs={refs}", file=sys.stderr)

    report: dict[str, Any] = {
        "base_url": args.base,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "session_id": client.session_id,
        "refs": refs,
        "n_tools_discovered": len(tool_names),
        "calls_per_tool": args.calls,
        "results": {},
    }

    for tool in tool_names:
        call_args = build_call_args(tool, refs)
        print(f"# benching {tool} args={call_args} ...", file=sys.stderr)
        res = bench_tool(client, tool, call_args, args.calls, args.sleep)
        summ = summarize(res)
        report["results"][tool] = summ
        print(
            f"#   cold={summ['cold_ms']}ms p50={summ['p50_ms']}ms p95={summ['p95_ms']}ms "
            f"verdict={summ['verdict']} errors={len(summ['errors'])}",
            file=sys.stderr,
        )
        if summ["rate_limited"]:
            print("#   RATE LIMITED — backing off 10s before next tool", file=sys.stderr)
            time.sleep(10)

    out_json = json.dumps(report, indent=2, default=str)
    if args.out:
        with open(args.out, "w") as f:
            f.write(out_json)
        print(f"# wrote report to {args.out}", file=sys.stderr)
    else:
        print(out_json)

    return 0


if __name__ == "__main__":
    sys.exit(main())
