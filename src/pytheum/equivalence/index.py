"""In-memory index of the verified Kalshi<->Polymarket equivalence dataset.

136,877 pairs loaded at first access (lazy singleton). The dataset lives at
datasets/equivalence-export.jsonl.gz (git-tracked). Path overridable via
PYTHEUM_EQUIVALENCE_PATH env var. Missing file -> empty index + file_missing flag,
never crashes the server.
"""
from __future__ import annotations

import gzip
import importlib.resources
import json
import logging
import os
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, ClassVar

logger = logging.getLogger(__name__)


def is_fungible_method(method: str | None) -> bool:
    """Return True when a pair's method indicates settlement-verified equivalence.

    Fungibility mapping
    -------------------
    FUNGIBLE — deterministic / structural / human-reviewed methods:

      *_match methods (game_match, game_title_match, game_datewindow_match,
        tennis_match, total_match, spread_match, btts_match, nrfi_match,
        player_prop_match, nfl_prop_match, mlb_prop_match, moneyline_outcome_match,
        ufc_ml_match, pga_top_match, etc.)
          — rule-based structural alignment; no probabilistic step.

      structured_key
          — exact (league, date, teams) key lookup; deterministic.

      blocked_deterministic
          — deterministic rule that previously rejected a pair but now accepts it
          after a validation step; same confidence class as *_match methods.

      award_match, election_match, macro_match, intl_game_title, baff_match,
      cannes_palmedor, cfb_conf_champ, emmy_winner, eurovision_match, f1_champ,
      house_party, lol_worlds, mayoral, nobel_peace, sb_perform, and other
      named deterministic event keys
          — deterministic per-event keying rules.

      human_adjudicated
          — expert-reviewed pair; treated as ground truth.

    NOT FUNGIBLE — probabilistic / LLM-judged methods:

      opus_backstop
          — Claude Opus judge; high precision but not a structural rule.

      llm_local
          — local LLM judge; same class.

      Any method string whose components include a token containing "llm" or
      equal to "opus_backstop" (handles combined strings like
      "blocked_deterministic,opus_backstop" — the pair's confirmation required
      an LLM step, so it is not purely deterministic).

    Unknown / None → NOT fungible (conservative default for unrecognised methods).
    """
    if not method or not method.strip():
        return False
    # Methods may be comma-joined (e.g. "blocked_deterministic,opus_backstop").
    # The pair is fungible only when NONE of its component tokens are LLM-judged.
    has_token = False
    for part in method.split(","):
        part = part.strip()
        if not part:
            continue
        has_token = True
        if "llm" in part or part == "opus_backstop":
            return False
    # A string that's non-empty but contains only separators/whitespace is treated
    # as unknown → NOT fungible (conservative default per the docstring).
    return has_token


_DATASET_FILENAME = "equivalence-export.jsonl.gz"


def _find_default_path() -> Path:
    """Resolve the default equivalence file path.

    Priority:
    1. PYTHEUM_EQUIVALENCE_PATH env var (absolute or relative to cwd)
    2. pytheum.datasets package data via importlib.resources (installed wheel)
    3. datasets/equivalence-export.jsonl.gz relative to cwd (repo root)
    4. <project-root>/datasets/equivalence-export.jsonl.gz relative to this file
    """
    env = os.environ.get("PYTHEUM_EQUIVALENCE_PATH")
    if env:
        return Path(env)
    # importlib.resources: resolves to the bundled copy inside the installed wheel
    # or the editable-install src tree, whichever is active.
    try:
        pkg_ref = importlib.resources.files("pytheum.datasets").joinpath(_DATASET_FILENAME)
        pkg_path = Path(str(pkg_ref))
        if pkg_path.exists():
            return pkg_path
    except Exception:
        pass
    # Fallback: cwd-relative (running from the repo root without installing)
    cwd_path = Path("datasets") / _DATASET_FILENAME
    if cwd_path.exists():
        return cwd_path
    # Dev fallback: this file is src/pytheum/equivalence/index.py → root 4 levels up
    return Path(__file__).parent.parent.parent.parent / "datasets" / _DATASET_FILENAME


class EquivalenceIndex:
    """In-memory lookup tables for the cross-venue equivalence dataset.

    Four lookup dicts keyed by kalshi_ticker, pm_gamma_id, pm_condition_id
    (lowercased), and pm_slug respectively. Values are lists of pair rows (list
    because in theory a ticker could map to multiple rows, though the dataset
    is effectively 1:1).
    """

    # Convenience group name -> set of bet_type values.  Callers may also pass
    # explicit bet_type names directly; these groups are aliases for the broad
    # sports slice and are resolved in browse().
    BET_TYPE_GROUPS: ClassVar[dict[str, set[str]]] = {
        "sports": {
            "moneyline", "moneyline_outcome", "moneyline_1h",
            "total", "total_1h",
            "spread", "spread_1h",
            "btts",
            "tennis_ml", "tennis_set1", "tennis_total", "tennis_set_total",
            "esports_map", "esports_series", "esports_total",
            "player_prop", "nfl_prop", "wc_prop", "mlb_prop",
            "goalscorer", "nrfi",
            "ufc_ml", "ufc_distance",
            "pga_top",
            "winter_olympics_gold",
        },
    }

    def __init__(self) -> None:
        self._rows: list[dict[str, Any]] = []
        self._by_kalshi_ticker: dict[str, list[dict[str, Any]]] = {}
        self._by_pm_gamma_id: dict[str, list[dict[str, Any]]] = {}
        self._by_pm_condition_id: dict[str, list[dict[str, Any]]] = {}  # always lowercase
        self._by_pm_slug: dict[str, list[dict[str, Any]]] = {}
        self.file_missing: bool = False
        self.load_error: str | None = None
        self.dataset_version: str | None = None  # ISO mtime of the source file

    @classmethod
    def load(cls, path: Path | None = None) -> EquivalenceIndex:
        """Load from path (default: auto-resolved). Fault-tolerant: any error
        returns an empty index with file_missing / load_error set; never raises."""
        idx = cls()
        fpath = path if path is not None else _find_default_path()
        try:
            mtime = fpath.stat().st_mtime
            idx.dataset_version = datetime.fromtimestamp(
                mtime, tz=UTC
            ).strftime("%Y-%m-%dT%H:%M:%SZ")
        except OSError:
            idx.file_missing = True
            logger.warning("equivalence: file not found at %s — feature degraded", fpath)
            return idx
        try:
            open_fn: Any = gzip.open if str(fpath).endswith(".gz") else open
            with open_fn(fpath, "rt", encoding="utf-8") as fh:
                for raw in fh:
                    raw = raw.strip()
                    if not raw:
                        continue
                    try:
                        row = json.loads(raw)
                    except json.JSONDecodeError:
                        continue
                    idx._rows.append(row)
                    kt = row.get("kalshi_ticker")
                    if kt:
                        idx._by_kalshi_ticker.setdefault(kt, []).append(row)
                    gid = row.get("pm_gamma_id")
                    if gid is not None:
                        idx._by_pm_gamma_id.setdefault(str(gid), []).append(row)
                    cid = row.get("pm_condition_id")
                    if cid:
                        idx._by_pm_condition_id.setdefault(cid.lower(), []).append(row)
                    slug = row.get("pm_slug")
                    if slug:
                        idx._by_pm_slug.setdefault(slug, []).append(row)
            logger.info("equivalence: loaded %d pairs from %s", len(idx._rows), fpath)
        except Exception as exc:
            idx.load_error = str(exc)
            logger.warning("equivalence: load failed (%s) — feature degraded", exc)
        return idx

    @property
    def pairs_loaded(self) -> int:
        return len(self._rows)

    def lookup(self, ref: str) -> tuple[list[dict[str, Any]], str]:
        """Look up equivalence rows for a market ref.

        Returns (rows, matched_via) where matched_via names the index key used.
        rows is empty when not found (matched_via == "none").

        Accepted ref forms:
        - "kalshi:<ticker>" or bare "<ticker>"
        - "polymarket:<gamma_id>" (numeric)
        - "polymarket:0x<condition_id>" or bare "0x<condition_id>"
        - "polymarket:<slug>" or bare "<slug>"
        """
        if not isinstance(ref, str) or not ref.strip():
            return [], "none"
        ref = ref.strip()
        head, sep, body = ref.partition(":")
        if sep and head.strip().lower() in ("kalshi", "polymarket", "manifold"):
            venue: str | None = head.strip().lower()
            body = body.strip()
        else:
            venue = None
            body = ref

        if venue == "kalshi":
            rows = self._by_kalshi_ticker.get(body, [])
            return (rows, "kalshi_ticker") if rows else ([], "none")

        if venue == "polymarket":
            if body.isdigit():
                rows = self._by_pm_gamma_id.get(body, [])
                if rows:
                    return rows, "pm_gamma_id"
            if body.lower().startswith("0x"):
                rows = self._by_pm_condition_id.get(body.lower(), [])
                if rows:
                    return rows, "pm_condition_id"
            rows = self._by_pm_slug.get(body, [])
            return (rows, "pm_slug") if rows else ([], "none")

        # No venue prefix — try each index in order
        rows = self._by_kalshi_ticker.get(body, [])
        if rows:
            return rows, "kalshi_ticker"
        if body.isdigit():
            rows = self._by_pm_gamma_id.get(body, [])
            if rows:
                return rows, "pm_gamma_id"
        if body.lower().startswith("0x"):
            rows = self._by_pm_condition_id.get(body.lower(), [])
            if rows:
                return rows, "pm_condition_id"
        rows = self._by_pm_slug.get(body, [])
        return (rows, "pm_slug") if rows else ([], "none")

    @property
    def bet_types_available(self) -> list[str]:
        """Sorted list of distinct bet_type values present in the loaded dataset."""
        seen: set[str] = set()
        for row in self._rows:
            bt = row.get("bet_type")
            if bt:
                seen.add(str(bt))
        return sorted(seen)

    def quality_stats(self) -> dict[str, Any]:
        """Tiered quality posture of the loaded dataset, computed in one pass.

        Splits pairs into the deterministic/structural/human-reviewed tier
        (settlement-verified, via is_fungible_method) vs the LLM-judged tier,
        and tallies methods + bet types. The transparency artifact behind
        /v1/quality + t_quality — every number is DERIVED from the shipped
        dataset, never asserted."""
        total = len(self._rows)
        methods: Counter[str] = Counter()
        bet_types: Counter[str] = Counter()
        fungible = 0
        for row in self._rows:
            method = row.get("method")
            methods[method or "unknown"] += 1
            bet_types[str(row.get("bet_type") or "unknown")] += 1
            if is_fungible_method(method):
                fungible += 1
        judged = total - fungible

        def _pct(n: int) -> float:
            return round(100.0 * n / total, 1) if total else 0.0

        return {
            "pairs_total": total,
            "dataset_version": self.dataset_version,
            "tiers": {
                "fungible": {
                    "pairs": fungible, "pct": _pct(fungible),
                    "note": "deterministic / structural / human-reviewed — settlement-verified equivalence",
                },
                "judged": {
                    "pairs": judged, "pct": _pct(judged),
                    "note": "LLM-adjudicated — high precision but confirm rules before treating as a fungible lock",
                },
            },
            "by_method": dict(methods.most_common(15)),
            "by_bet_type": dict(bet_types.most_common(25)),
            "bet_types_total": len(bet_types),
        }

    def browse(
        self,
        *,
        bet_types: set[str] | None = None,
        query_substr: str | None = None,
        league: str | None = None,
        game_date: str | None = None,
        fungible_only: bool = False,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[dict[str, Any]], int]:
        """Return a paginated slice of rows, optionally filtered.

        Parameters
        ----------
        bet_types:
            When provided, only rows whose ``bet_type`` is in this set are
            returned.  Group names from ``BET_TYPE_GROUPS`` (e.g. ``"sports"``)
            are NOT resolved here — callers should expand them before calling.
        query_substr:
            Case-insensitive substring filter over both ``kalshi_title`` and
            ``pm_title``.
        league:
            Case-insensitive exact match on the row's ``league`` field.  Rows
            that lack a ``league`` field are **excluded** when this filter is
            active.
        game_date:
            Exact match on the row's ``game_date`` field (YYYY-MM-DD). Rows
            that lack a ``game_date`` field are **excluded** when this filter
            is active.
        fungible_only:
            When True, only rows whose ``method`` qualifies as a deterministic
            structural or human-adjudicated match (see ``is_fungible_method``)
            are included.  LLM-judged pairs (``opus_backstop``, ``llm_local``)
            are excluded.
        limit:
            Maximum rows to return (after filtering).
        offset:
            Zero-based starting position into the filtered result.

        Returns
        -------
        (rows, total) where ``total`` is the full filtered count (before
        pagination) so callers can compute page counts.
        """
        needle = query_substr.lower() if query_substr else None
        league_lower = league.strip().lower() if league else None

        filtered: list[dict[str, Any]] = []
        for row in self._rows:
            if bet_types is not None:
                bt = row.get("bet_type", "")
                if bt not in bet_types:
                    continue
            if needle is not None:
                kt = (row.get("kalshi_title") or "").lower()
                pt = (row.get("pm_title") or "").lower()
                if needle not in kt and needle not in pt:
                    continue
            if league_lower is not None:
                row_league = row.get("league")
                if not row_league:
                    continue  # exclude rows without the field
                if str(row_league).strip().lower() != league_lower:
                    continue
            if game_date is not None:
                row_date = row.get("game_date")
                if not row_date:
                    continue  # exclude rows without the field
                if str(row_date).strip() != game_date.strip():
                    continue
            if fungible_only and not is_fungible_method(row.get("method")):
                continue
            filtered.append(row)

        total = len(filtered)
        page = filtered[offset: offset + limit]
        return page, total

    def leagues_available(self, *, max_values: int = 50) -> list[str]:
        """Return up to *max_values* distinct league values present in loaded rows.

        Only rows that carry a non-empty ``league`` field are counted.  The
        result is sorted alphabetically and capped at *max_values* so callers
        can always surface it in a meta block without blowing response size.
        """
        seen: set[str] = set()
        for row in self._rows:
            lg = row.get("league")
            if lg:
                seen.add(str(lg))
            if len(seen) >= max_values:
                break
        return sorted(seen)[:max_values]


# Module-level lazy singleton
_singleton: EquivalenceIndex | None = None


def get_index() -> EquivalenceIndex:
    """Return the module-level equivalence index (lazy-loaded on first call)."""
    global _singleton
    if _singleton is None:
        _singleton = EquivalenceIndex.load()
    return _singleton
