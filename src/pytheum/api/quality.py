"""GET /v1/quality — keyless dataset quality + integrity transparency.

The "verify before you pay" artifact: exposes, from the shipped equivalence
dataset, the deterministic-vs-judged tier split, method/bet-type composition,
the build-time integrity invariants we enforce, and an HONEST precision posture.

Design discipline: every count is DERIVED from the loaded dataset; the integrity
block states only invariants the build gates actually enforce; and the audited
precision is TIER-SCOPED + point-in-time (not a single whole-corpus headline %)
— the judged tier (LLM slice) is exhaustively audited; the deterministic tier is
lint-gated with its sampled audit still pending (`deterministic_tier_pct: null`).

Response shape
--------------
{
  "pairs_total": int,
  "dataset_version": str|null,
  "tiers": {"fungible": {...}, "judged": {...}},
  "by_method": {...}, "by_bet_type": {...}, "bet_types_total": int,
  "integrity": {"enforced_at_build": true, "invariants": [...], "note": str},
  "precision": {"fungible_tier": str, "judged_tier": str,
                "audited": {"judged_tier_pct": float, "judged_tier_ci95": [lo,hi],
                            "judged_tier_n": int, "deterministic_tier_pct": null,
                            "as_of": str, "method": str, "methodology_doc": str},
                "note": str},
  "service": {"version": str, "now": ISO-8601}
}
"""
from __future__ import annotations

import time
from datetime import UTC, datetime
from typing import Any

_CACHE_TTL_S = 60.0
_cache: tuple[float, dict[str, Any]] | None = None

# Invariants the build gates ENFORCE before the dataset is allowed to ship
# (tools.build_dataset integrity gate + lint_curated + lint_structured). Stated,
# not re-verified at serve time — the published artifact only exists if these passed.
_INTEGRITY_INVARIANTS = [
    "1:1 — no Kalshi or Polymarket market id appears in more than one pair",
    "single-slice-per-id — each market belongs to exactly one bet-type slice",
    "line-invariant — every line-keyed pair (totals/spreads/team-totals/…) shows its line in the Polymarket title",
    "abbrev-equality / name-alignment — structural pairs re-derive to matching team/entity keys",
    "same-city disambiguation — Kalshi 'City <letter>' agrees with the Polymarket nickname",
]

_service_version: str | None = None


def _get_version() -> str:
    global _service_version
    if _service_version is None:
        try:
            import importlib.metadata
            _service_version = importlib.metadata.version("pytheum")
        except Exception:
            _service_version = "dev"
    return _service_version


async def handle_quality(
    query: dict[str, str],  # kept for signature consistency; unused today
    *,
    equivalence: Any = None,
) -> tuple[int, dict[str, Any]]:
    """GET /v1/quality handler. ``equivalence`` is an optional duck-typed index;
    falls back to the module singleton when None."""
    global _cache
    if _cache is not None and time.monotonic() - _cache[0] < _CACHE_TTL_S:
        return 200, _cache[1]

    if equivalence is None:
        try:
            from pytheum.equivalence.index import get_index
            equivalence = get_index()
        except Exception:
            equivalence = None

    stats = getattr(equivalence, "quality_stats", None)
    body: dict[str, Any] = stats() if callable(stats) else {
        "pairs_total": 0, "dataset_version": None,
        "tiers": {}, "by_method": {}, "by_bet_type": {}, "bet_types_total": 0,
    }

    body["integrity"] = {
        "enforced_at_build": True,
        "invariants": _INTEGRITY_INVARIANTS,
        "note": ("Verified by the build-time gates (build_dataset integrity + "
                 "lint_curated + lint_structured). The published dataset ships "
                 "only when all hard gates pass; these are not re-checked per request."),
    }
    body["precision"] = {
        "fungible_tier": ("deterministic / structural — gated to ~0 wrong by the "
                          "structural lint (line + abbrev + name-alignment invariants); "
                          "not a probabilistic estimate. Sampled audit pending."),
        "judged_tier": ("LLM-adjudicated — exhaustively audited 2026-06-22: 98.7% "
                        "precision (95% CI 95.4-99.7%, n=155); the 2 false positives "
                        "found (macro rate-bucket mismatches) were denylisted."),
        # Tier-scoped audited numbers. Point-in-time + per-tier ON PURPOSE — there
        # is no single whole-corpus headline %% (that would hide which tier was
        # measured). The judged tier (the LLM slice where FPs concentrate) was
        # audited exhaustively; the deterministic tier (99.9% of pairs) is
        # structurally gated, not yet sampled-audited.
        "audited": {
            "judged_tier_pct": 98.7,
            "judged_tier_ci95": [95.4, 99.7],
            "judged_tier_n": 155,
            "deterministic_tier_pct": None,  # lint-gated; sampled audit pending
            "as_of": "2026-06-22",
            "method": ("exhaustive in-context adjudication of all judged pairs vs a "
                       "strict same-outcome rubric (default not-equivalent on doubt)"),
            "methodology_doc": "docs/research/2026-06-22-judged-tier-precision-audit.md",
        },
        "note": ("audited figures are point-in-time and tier-scoped; the judged-tier "
                 "%% is the AS-AUDITED precision (the 2 FPs found were then remediated, "
                 "so the shipped judged tier is cleaner than the figure implies)."),
    }
    body["service"] = {
        "version": _get_version(),
        "now": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }

    _cache = (time.monotonic(), body)
    return 200, body
