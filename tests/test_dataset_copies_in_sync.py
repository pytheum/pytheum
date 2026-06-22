"""Guard against the 2026-06-22 deploy regression.

The equivalence export is tracked in TWO places — the public ``datasets/`` dir and the
``pytheum.datasets`` package-data copy that ships in the wheel. ``_find_default_path()``
prefers the importlib package-data copy, so if a sync updates only the root copy the
serving index silently loads stale data (PR #8 updated root only → the box index degraded
to a 2k-row stale copy). These tests fail the moment the two diverge or the loader-resolved
copy is unexpectedly small.
"""

from __future__ import annotations

import gzip
import hashlib
import importlib.resources
from pathlib import Path

_FILENAME = "equivalence-export.jsonl.gz"
_MIN_ROWS = 139_000  # current build is 139,145; guards against a stale/truncated copy
_ROOT = Path(__file__).resolve().parent.parent / "datasets" / _FILENAME


def _sha256(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def test_root_and_packaged_export_are_identical() -> None:
    pkg = Path(str(importlib.resources.files("pytheum.datasets").joinpath(_FILENAME)))
    assert _ROOT.exists(), f"missing root export {_ROOT}"
    assert pkg.exists(), f"missing package-data export {pkg}"
    # Byte-identical → a sync that touches only one path can never silently regress.
    assert _sha256(_ROOT) == _sha256(pkg), (
        "root datasets/ and pytheum.datasets package copy diverged — sync BOTH "
        "(the loader prefers the package copy; updating only root degrades the index)"
    )


def test_loader_resolved_export_is_fresh() -> None:
    from pytheum.equivalence.index import _find_default_path

    resolved = _find_default_path()
    with gzip.open(resolved, "rt", encoding="utf-8") as fh:
        rows = sum(1 for _ in fh)
    assert rows >= _MIN_ROWS, (
        f"loader resolved {resolved} with only {rows} rows (< {_MIN_ROWS}) — "
        "likely a stale package-data copy"
    )
