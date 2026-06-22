"""Coverage for the _find_default_path resolvers in related/equivalence indexes.

Exercises the env-var branch, the importlib.resources success path, the
cwd-relative fallback, and the dev (file-relative) fallback for both
RelatedIndex and EquivalenceIndex resolvers, plus their RELATION/BET_TYPE
group constants.
"""
from __future__ import annotations

import importlib.resources
from pathlib import Path
from typing import Any

import pytest

from pytheum.equivalence import index as eq_mod
from pytheum.related import index as rel_mod

# --------------------------------------------------------------------------- #
# related._find_default_path
# --------------------------------------------------------------------------- #


def test_related_path_env_var(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PYTHEUM_RELATED_PATH", "/tmp/custom-related.jsonl.gz")
    assert rel_mod._find_default_path() == Path("/tmp/custom-related.jsonl.gz")


def test_related_path_importlib_resources(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("PYTHEUM_RELATED_PATH", raising=False)
    pkg_file = tmp_path / rel_mod._DATASET_FILENAME
    pkg_file.write_bytes(b"")

    class _Ref:
        def joinpath(self, name: str) -> Path:
            return pkg_file

    monkeypatch.setattr(importlib.resources, "files", lambda pkg: _Ref())
    assert rel_mod._find_default_path() == pkg_file


def test_related_path_cwd_fallback(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("PYTHEUM_RELATED_PATH", raising=False)

    def _boom(pkg: str) -> Any:
        raise RuntimeError("no package data")

    monkeypatch.setattr(importlib.resources, "files", _boom)
    monkeypatch.chdir(tmp_path)
    (tmp_path / "datasets").mkdir()
    cwd_file = tmp_path / "datasets" / rel_mod._DATASET_FILENAME
    cwd_file.write_bytes(b"")
    assert rel_mod._find_default_path() == Path("datasets") / rel_mod._DATASET_FILENAME


def test_related_path_dev_fallback(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("PYTHEUM_RELATED_PATH", raising=False)
    monkeypatch.setattr(
        importlib.resources, "files",
        lambda pkg: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    monkeypatch.chdir(tmp_path)  # no datasets/ dir here → dev fallback
    out = rel_mod._find_default_path()
    # The dev fallback is rooted at the source file, ends with the dataset name.
    assert out.name == rel_mod._DATASET_FILENAME
    assert "datasets" in out.parts


# --------------------------------------------------------------------------- #
# equivalence._find_default_path
# --------------------------------------------------------------------------- #


def test_eq_path_env_var(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PYTHEUM_EQUIVALENCE_PATH", "/tmp/custom-eq.jsonl.gz")
    assert eq_mod._find_default_path() == Path("/tmp/custom-eq.jsonl.gz")


def test_eq_path_importlib_resources(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("PYTHEUM_EQUIVALENCE_PATH", raising=False)
    pkg_file = tmp_path / eq_mod._DATASET_FILENAME
    pkg_file.write_bytes(b"")

    class _Ref:
        def joinpath(self, name: str) -> Path:
            return pkg_file

    monkeypatch.setattr(importlib.resources, "files", lambda pkg: _Ref())
    assert eq_mod._find_default_path() == pkg_file


def test_eq_path_cwd_fallback(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("PYTHEUM_EQUIVALENCE_PATH", raising=False)
    monkeypatch.setattr(
        importlib.resources, "files",
        lambda pkg: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    monkeypatch.chdir(tmp_path)
    (tmp_path / "datasets").mkdir()
    cwd_file = tmp_path / "datasets" / eq_mod._DATASET_FILENAME
    cwd_file.write_bytes(b"")
    assert eq_mod._find_default_path() == Path("datasets") / eq_mod._DATASET_FILENAME


def test_eq_path_dev_fallback(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("PYTHEUM_EQUIVALENCE_PATH", raising=False)
    monkeypatch.setattr(
        importlib.resources, "files",
        lambda pkg: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    monkeypatch.chdir(tmp_path)
    out = eq_mod._find_default_path()
    assert out.name == eq_mod._DATASET_FILENAME
    assert "datasets" in out.parts


def test_eq_path_importlib_nonexistent_falls_through(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """importlib resolves a path that does NOT exist → skip to cwd/dev fallback."""
    monkeypatch.delenv("PYTHEUM_EQUIVALENCE_PATH", raising=False)
    ghost = tmp_path / "ghost" / eq_mod._DATASET_FILENAME  # never created

    class _Ref:
        def joinpath(self, name: str) -> Path:
            return ghost

    monkeypatch.setattr(importlib.resources, "files", lambda pkg: _Ref())
    monkeypatch.chdir(tmp_path)
    (tmp_path / "datasets").mkdir()
    cwd_file = tmp_path / "datasets" / eq_mod._DATASET_FILENAME
    cwd_file.write_bytes(b"")
    assert eq_mod._find_default_path() == Path("datasets") / eq_mod._DATASET_FILENAME
