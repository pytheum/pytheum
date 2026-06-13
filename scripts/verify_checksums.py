#!/usr/bin/env python3
"""Verify SHA-256 sidecar files for every .gz artifact in datasets/.

Usage:
    python scripts/verify_checksums.py [datasets-dir]

For each ``<name>.gz.sha256`` sidecar found directly under *datasets-dir*
(default: ``datasets/`` relative to this script's repo root) the
corresponding ``.gz`` file is re-hashed and compared against the stored
digest.  Exits non-zero if any mismatch is detected or if a sidecar's
target file is missing.
"""
from __future__ import annotations

import sys
from pathlib import Path

# Re-use the hashing implementation so both scripts stay in sync.
_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT / "scripts"))

from gen_checksums import compute_sha256  # noqa: E402


def verify_checksums(directory: Path) -> list[str]:
    """Verify all ``.sha256`` sidecars in *directory*.

    Returns a list of error message strings (empty list = all OK).
    """
    errors: list[str] = []
    sidecars = sorted(directory.glob("*.gz.sha256"))
    if not sidecars:
        return errors  # nothing to verify is not an error

    for sidecar in sidecars:
        # Sidecar format: "<digest>  <filename>\n"
        line = sidecar.read_text(encoding="utf-8").strip()
        parts = line.split(None, 1)
        if len(parts) != 2:
            errors.append(f"{sidecar.name}: malformed sidecar (expected '<digest>  <name>')")
            continue
        expected_digest, recorded_name = parts

        gz_path = directory / recorded_name
        if not gz_path.exists():
            errors.append(f"{recorded_name}: file missing (sidecar present)")
            continue

        actual_digest = compute_sha256(gz_path)
        if actual_digest != expected_digest:
            errors.append(
                f"{recorded_name}: digest mismatch\n"
                f"  expected: {expected_digest}\n"
                f"  actual:   {actual_digest}"
            )
        else:
            print(f"OK  {recorded_name}  ({actual_digest[:12]}…)")

    return errors


def _default_datasets_dir() -> Path:
    return _REPO_ROOT / "datasets"


def main(argv: list[str] | None = None) -> int:
    args = argv if argv is not None else sys.argv[1:]
    directory = Path(args[0]) if args else _default_datasets_dir()
    if not directory.is_dir():
        print(f"error: {directory} is not a directory", file=sys.stderr)
        return 1
    errors = verify_checksums(directory)
    if errors:
        print("\nCHECKSUM FAILURES:", file=sys.stderr)
        for e in errors:
            print(f"  {e}", file=sys.stderr)
        return 1
    print("\nAll checksums verified OK.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
