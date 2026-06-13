"""In-memory index of the correlated (but NOT settlement-equivalent) cross-venue dataset.

1,097 pairs loaded at first access (lazy singleton). The dataset lives at
datasets/related-export.jsonl.gz (git-tracked). Path overridable via
PYTHEUM_RELATED_PATH env var. Missing file -> empty index + file_missing flag,
never crashes the server.

Each row carries kalshi_ref/ticker/title, pm_ref/gamma_id/condition_id/slug/title,
plus the correlation metadata: relation, asset, date, kalshi_band, pm_band,
basis_note.
"""
from __future__ import annotations

import gzip
import json
import logging
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, ClassVar

logger = logging.getLogger(__name__)


def _find_default_path() -> Path:
    """Resolve the default related file path.

    Priority:
    1. PYTHEUM_RELATED_PATH env var (absolute or relative to cwd)
    2. datasets/related-export.jsonl.gz relative to cwd (production)
    3. <project-root>/datasets/related-export.jsonl.gz relative to this file
    """
    env = os.environ.get("PYTHEUM_RELATED_PATH")
    if env:
        return Path(env)
    cwd_path = Path("datasets") / "related-export.jsonl.gz"
    if cwd_path.exists():
        return cwd_path
    # this file: src/pytheum/related/index.py -> project root is 4 parents up
    return Path(__file__).parent.parent.parent.parent / "datasets" / "related-export.jsonl.gz"


class RelatedIndex:
    """In-memory lookup tables for the cross-venue related (correlated) dataset.

    Four lookup dicts keyed by kalshi_ticker, pm_gamma_id, pm_condition_id
    (lowercased), and pm_slug respectively. Values are lists of pair rows (a
    single ticker may map to multiple correlated counterparts, e.g. several
    Kalshi bands pointing at the same PM market).

    Each row includes the correlation metadata: relation, asset, date,
    kalshi_band, pm_band, basis_note.
    """

    # Convenience group name -> set of relation values. Callers may also pass
    # explicit relation names directly; these groups are aliases for the broad
    # macro/crypto slice and are resolved in browse().
    RELATION_GROUPS: ClassVar[dict[str, set[str]]] = {
        "crypto": {
            "same_asset_date_contained",
            "same_asset_date_adjacent",
            "same_asset_date_overlapping",
        },
        "macro": {
            "same_event_different_band",
            "same_event_different_source",
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
    def load(cls, path: Path | None = None) -> RelatedIndex:
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
            logger.warning("related: file not found at %s — feature degraded", fpath)
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
            logger.info("related: loaded %d pairs from %s", len(idx._rows), fpath)
        except Exception as exc:
            idx.load_error = str(exc)
            logger.warning("related: load failed (%s) — feature degraded", exc)
        return idx

    @property
    def pairs_loaded(self) -> int:
        return len(self._rows)

    def lookup(self, ref: str) -> tuple[list[dict[str, Any]], str]:
        """Look up related rows for a market ref.

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
    def relations_available(self) -> list[str]:
        """Sorted list of distinct relation values present in the loaded dataset."""
        seen: set[str] = set()
        for row in self._rows:
            rel = row.get("relation")
            if rel:
                seen.add(str(rel))
        return sorted(seen)

    def browse(
        self,
        *,
        relations: set[str] | None = None,
        query_substr: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[dict[str, Any]], int]:
        """Return a paginated slice of rows, optionally filtered.

        Parameters
        ----------
        relations:
            When provided, only rows whose ``relation`` is in this set are
            returned.  Group names from ``RELATION_GROUPS`` (e.g. ``"crypto"``)
            are NOT resolved here — callers should expand them before calling.
        query_substr:
            Case-insensitive substring filter over both ``kalshi_title`` and
            ``pm_title``.
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

        filtered: list[dict[str, Any]] = []
        for row in self._rows:
            if relations is not None:
                rel = row.get("relation", "")
                if rel not in relations:
                    continue
            if needle is not None:
                kt = (row.get("kalshi_title") or "").lower()
                pt = (row.get("pm_title") or "").lower()
                if needle not in kt and needle not in pt:
                    continue
            filtered.append(row)

        total = len(filtered)
        page = filtered[offset: offset + limit]
        return page, total


# Module-level lazy singleton
_singleton: RelatedIndex | None = None


def get_index() -> RelatedIndex:
    """Return the module-level related index (lazy-loaded on first call)."""
    global _singleton
    if _singleton is None:
        _singleton = RelatedIndex.load()
    return _singleton
