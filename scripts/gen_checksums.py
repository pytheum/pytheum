#!/usr/bin/env python3
"""Generate SHA-256 sidecar files for every .gz artifact in datasets/.

Usage:
    python scripts/gen_checksums.py [datasets-dir]

For each ``<name>.gz`` found directly under *datasets-dir* (default:
``datasets/`` relative to this script's repo root) a ``<name>.gz.sha256``
sidecar is written containing the hex digest followed by a newline.

The sidecar format matches the ``sha256sum`` / ``shasum -a 256`` convention
so the files can be verified with standard POSIX tools as well as with
``scripts/verify_checksums.py``.
"""
from __future__ import annotations

import hashlib
import sys
from pathlib import Path


def compute_sha256(path: Path) -> str:
    """Return the lowercase hex SHA-256 digest of *path*."""
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def gen_checksums(directory: Path) -> dict[str, str]:
    """Write ``.sha256`` sidecars for every ``.gz`` in *directory*.

    Returns a mapping of ``filename -> hex_digest`` for all files processed.
    """
    results: dict[str, str] = {}
    for gz_path in sorted(directory.glob("*.gz")):
        digest = compute_sha256(gz_path)
        sidecar = gz_path.with_suffix(".gz.sha256")
        sidecar.write_text(f"{digest}  {gz_path.name}\n", encoding="utf-8")
        results[gz_path.name] = digest
        print(f"wrote {sidecar.name}  ({digest[:12]}…)")
    return results


def _default_datasets_dir() -> Path:
    return Path(__file__).resolve().parent.parent / "datasets"


def main(argv: list[str] | None = None) -> int:
    args = argv if argv is not None else sys.argv[1:]
    directory = Path(args[0]) if args else _default_datasets_dir()
    if not directory.is_dir():
        print(f"error: {directory} is not a directory", file=sys.stderr)
        return 1
    results = gen_checksums(directory)
    if not results:
        print("no .gz files found — nothing to do")
    else:
        print(f"\n{len(results)} sidecar(s) written to {directory}/")
    return 0


if __name__ == "__main__":
    sys.exit(main())
