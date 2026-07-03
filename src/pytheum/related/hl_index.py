"""In-memory index of the Hyperliquid related (correlated) cross-venue dataset.

Rows are loaded at first access (lazy singleton) from a local export. No data
ships in the package: the path is taken from the PYTHEUM_HL_RELATED_PATH env var.
Missing file -> empty index + file_missing flag, never crashes the server.

Each row is venue-explicit: a 2-element ``legs`` list — exactly one hyperliquid
leg plus one kalshi-or-polymarket leg. Every leg carries ``venue``, ``ref``,
``native_id``, ``title``; Polymarket legs add ``gamma_id`` + ``slug``;
Hyperliquid legs add ``implied_yes`` (0-1) + ``as_of`` (ISO, mint-time daily
snapshot — NOT a live quote). Rows also carry flattened ``<venue>_ref`` /
``<venue>_native_id`` / ``<venue>_title`` fields plus pass-through metadata:
``tier`` ("related"), ``relation``, ``settlement``, ``country``, ``asset``,
``date``, ``settle_delta_hours``, ``basis_note``, etc.
"""
from __future__ import annotations

import gzip
import importlib.resources
import json
import logging
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


_DATASET_FILENAME = "hl-related-export.jsonl.gz"


def _find_default_path() -> Path:
    """Resolve the default HL-related file path.

    Priority:
    1. PYTHEUM_HL_RELATED_PATH env var (absolute or relative to cwd)
    2. pytheum.datasets package data via importlib.resources (installed wheel)
    3. datasets/hl-related-export.jsonl.gz relative to cwd (repo root)
    4. <project-root>/datasets/hl-related-export.jsonl.gz relative to this file
    """
    env = os.environ.get("PYTHEUM_HL_RELATED_PATH")
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
    # Dev fallback: this file is src/pytheum/related/hl_index.py → root 4 levels up
    return Path(__file__).parent.parent.parent.parent / "datasets" / _DATASET_FILENAME


class HLRelatedIndex:
    """In-memory lookup tables for the Hyperliquid related (correlated) dataset.

    Five lookup dicts keyed by every leg identifier: kalshi ticker, pm gamma_id,
    pm condition_id (lowercased — matching the equivalence index's
    normalization), pm slug, and hyperliquid native_id. Values are lists of
    rows verbatim (a single market may map to multiple HL counterparts).
    """

    def __init__(self) -> None:
        self._rows: list[dict[str, Any]] = []
        self._by_kalshi_ticker: dict[str, list[dict[str, Any]]] = {}
        self._by_pm_gamma_id: dict[str, list[dict[str, Any]]] = {}
        self._by_pm_condition_id: dict[str, list[dict[str, Any]]] = {}  # always lowercase
        self._by_pm_slug: dict[str, list[dict[str, Any]]] = {}
        self._by_hl_native_id: dict[str, list[dict[str, Any]]] = {}
        self.file_missing: bool = False
        self.load_error: str | None = None
        self.dataset_version: str | None = None  # _meta version, else ISO mtime

    @classmethod
    def load(cls, path: Path | None = None) -> HLRelatedIndex:
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
            logger.warning("hl_related: file not found at %s — feature degraded", fpath)
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
                    if not isinstance(row, dict):
                        continue
                    if "_meta" in row:
                        # Metadata line, not a pair: prefer its version string
                        # over the file-mtime fallback.
                        meta = row["_meta"]
                        version: Any = None
                        if isinstance(meta, dict):
                            version = meta.get("dataset_version") or meta.get("version")
                        elif isinstance(meta, str):
                            version = meta
                        if version:
                            idx.dataset_version = str(version)
                        continue
                    idx._rows.append(row)
                    idx._index_row(row)
            logger.info("hl_related: loaded %d pairs from %s", len(idx._rows), fpath)
        except Exception as exc:
            idx.load_error = str(exc)
            logger.warning("hl_related: load failed (%s) — feature degraded", exc)
        return idx

    def _index_row(self, row: dict[str, Any]) -> None:
        """Register *row* under every leg identifier it carries."""
        legs = row.get("legs")
        if isinstance(legs, list) and legs:
            for leg in legs:
                if isinstance(leg, dict):
                    self._index_leg(row, leg)
            return
        # Flattened-fields fallback (rows without a usable legs list)
        kt = row.get("kalshi_native_id")
        if kt:
            self._by_kalshi_ticker.setdefault(str(kt), []).append(row)
        cid = row.get("polymarket_native_id")
        if cid:
            self._by_pm_condition_id.setdefault(str(cid).lower(), []).append(row)
        hid = row.get("hyperliquid_native_id")
        if hid:
            self._by_hl_native_id.setdefault(str(hid), []).append(row)

    def _index_leg(self, row: dict[str, Any], leg: dict[str, Any]) -> None:
        venue = str(leg.get("venue") or "").lower()
        native_id = leg.get("native_id")
        if venue == "kalshi":
            if native_id:
                self._by_kalshi_ticker.setdefault(str(native_id), []).append(row)
        elif venue == "polymarket":
            gid = leg.get("gamma_id")
            if gid is not None:
                self._by_pm_gamma_id.setdefault(str(gid), []).append(row)
            if native_id:  # native_id is the condition_id for PM legs
                self._by_pm_condition_id.setdefault(str(native_id).lower(), []).append(row)
            slug = leg.get("slug")
            if slug:
                self._by_pm_slug.setdefault(str(slug), []).append(row)
        elif venue == "hyperliquid" and native_id:
            self._by_hl_native_id.setdefault(str(native_id), []).append(row)

    @property
    def pairs_loaded(self) -> int:
        return len(self._rows)

    def rows_for_ref(self, ref_value: str) -> list[dict[str, Any]]:
        """Return the HL-related rows (verbatim) for a market ref, else [].

        Accepted ref forms:
        - "kalshi:<ticker>" or bare "<ticker>"
        - "polymarket:<gamma_id>" (numeric)
        - "polymarket:0x<condition_id>" or bare "0x<condition_id>"
        - "polymarket:<slug>" or bare "<slug>"
        - "hyperliquid:<native_id>" or bare "<native_id>"
        """
        if not isinstance(ref_value, str) or not ref_value.strip():
            return []
        ref = ref_value.strip()
        head, sep, body = ref.partition(":")
        if sep and head.strip().lower() in ("kalshi", "polymarket", "hyperliquid", "manifold"):
            venue: str | None = head.strip().lower()
            body = body.strip()
        else:
            venue = None
            body = ref

        if venue == "kalshi":
            return self._by_kalshi_ticker.get(body, [])

        if venue == "hyperliquid":
            return self._by_hl_native_id.get(body, [])

        if venue == "polymarket":
            if body.isdigit():
                rows = self._by_pm_gamma_id.get(body, [])
                if rows:
                    return rows
            if body.lower().startswith("0x"):
                rows = self._by_pm_condition_id.get(body.lower(), [])
                if rows:
                    return rows
            return self._by_pm_slug.get(body, [])

        # No venue prefix — try each index in order
        rows = self._by_kalshi_ticker.get(body, [])
        if rows:
            return rows
        if body.isdigit():
            rows = self._by_pm_gamma_id.get(body, [])
            if rows:
                return rows
        if body.lower().startswith("0x"):
            rows = self._by_pm_condition_id.get(body.lower(), [])
            if rows:
                return rows
        rows = self._by_pm_slug.get(body, [])
        if rows:
            return rows
        return self._by_hl_native_id.get(body, [])


# Module-level lazy singleton
_singleton: HLRelatedIndex | None = None


def get_index() -> HLRelatedIndex:
    """Return the module-level HL-related index (lazy-loaded on first call)."""
    global _singleton
    if _singleton is None:
        _singleton = HLRelatedIndex.load()
    return _singleton
