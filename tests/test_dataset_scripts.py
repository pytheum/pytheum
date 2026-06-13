"""Unit tests for scripts/gen_checksums.py and scripts/verify_checksums.py.

Uses tmp_path fixtures to exercise the full gen → verify cycle including
corruption detection, missing-file detection, and malformed-sidecar detection.
"""
from __future__ import annotations

import hashlib
from pathlib import Path

import gen_checksums  # added to sys.path via conftest.py
import verify_checksums

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _write_gz(directory: Path, name: str, content: bytes) -> Path:
    p = directory / name
    p.write_bytes(content)
    return p


# ---------------------------------------------------------------------------
# gen_checksums tests
# ---------------------------------------------------------------------------


def test_gen_checksums_creates_sidecar(tmp_path: Path) -> None:
    _write_gz(tmp_path, "test-export.gz", b"fake gzip content")
    results = gen_checksums.gen_checksums(tmp_path)
    sidecar = tmp_path / "test-export.gz.sha256"
    assert sidecar.exists()
    assert "test-export.gz" in results
    assert results["test-export.gz"] == _sha256(b"fake gzip content")


def test_gen_checksums_sidecar_format(tmp_path: Path) -> None:
    _write_gz(tmp_path, "eq.gz", b"data")
    gen_checksums.gen_checksums(tmp_path)
    sidecar = tmp_path / "eq.gz.sha256"
    line = sidecar.read_text().strip()
    digest, name = line.split(None, 1)
    assert name == "eq.gz"
    assert len(digest) == 64  # SHA-256 hex


def test_gen_checksums_empty_dir_returns_empty(tmp_path: Path) -> None:
    results = gen_checksums.gen_checksums(tmp_path)
    assert results == {}


def test_gen_checksums_multiple_files(tmp_path: Path) -> None:
    _write_gz(tmp_path, "a.gz", b"aaa")
    _write_gz(tmp_path, "b.gz", b"bbb")
    results = gen_checksums.gen_checksums(tmp_path)
    assert set(results.keys()) == {"a.gz", "b.gz"}


def test_compute_sha256_matches_stdlib(tmp_path: Path) -> None:
    p = tmp_path / "data.bin"
    p.write_bytes(b"\x00" * 1000)
    assert gen_checksums.compute_sha256(p) == hashlib.sha256(b"\x00" * 1000).hexdigest()


# ---------------------------------------------------------------------------
# verify_checksums tests
# ---------------------------------------------------------------------------


def test_verify_clean_returns_no_errors(tmp_path: Path) -> None:
    _write_gz(tmp_path, "eq.gz", b"content")
    gen_checksums.gen_checksums(tmp_path)
    errors = verify_checksums.verify_checksums(tmp_path)
    assert errors == []


def test_verify_detects_corrupted_file(tmp_path: Path) -> None:
    gz = _write_gz(tmp_path, "eq.gz", b"original")
    gen_checksums.gen_checksums(tmp_path)
    # Corrupt the file after generating the sidecar.
    gz.write_bytes(b"corrupted")
    errors = verify_checksums.verify_checksums(tmp_path)
    assert any("digest mismatch" in e for e in errors)


def test_verify_detects_missing_file(tmp_path: Path) -> None:
    _write_gz(tmp_path, "eq.gz", b"data")
    gen_checksums.gen_checksums(tmp_path)
    # Remove the artifact but keep the sidecar.
    (tmp_path / "eq.gz").unlink()
    errors = verify_checksums.verify_checksums(tmp_path)
    assert any("missing" in e for e in errors)


def test_verify_empty_dir_returns_no_errors(tmp_path: Path) -> None:
    errors = verify_checksums.verify_checksums(tmp_path)
    assert errors == []


def test_verify_malformed_sidecar(tmp_path: Path) -> None:
    sidecar = tmp_path / "bad.gz.sha256"
    sidecar.write_text("not-a-valid-line\n", encoding="utf-8")
    errors = verify_checksums.verify_checksums(tmp_path)
    assert any("malformed" in e for e in errors)


# ---------------------------------------------------------------------------
# CLI entry points (main())
# ---------------------------------------------------------------------------


def test_gen_checksums_main_no_gz_exits_zero(tmp_path: Path) -> None:
    rc = gen_checksums.main([str(tmp_path)])
    assert rc == 0


def test_gen_checksums_main_bad_dir_exits_one() -> None:
    rc = gen_checksums.main(["/nonexistent/path/xyz"])
    assert rc == 1


def test_verify_checksums_main_clean_exits_zero(tmp_path: Path) -> None:
    _write_gz(tmp_path, "data.gz", b"bytes")
    gen_checksums.gen_checksums(tmp_path)
    rc = verify_checksums.main([str(tmp_path)])
    assert rc == 0


def test_verify_checksums_main_corrupted_exits_one(tmp_path: Path) -> None:
    gz = _write_gz(tmp_path, "data.gz", b"bytes")
    gen_checksums.gen_checksums(tmp_path)
    gz.write_bytes(b"tampered")
    rc = verify_checksums.main([str(tmp_path)])
    assert rc == 1
