# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- `pytheum.routing`: `Router` (pattern dispatcher) and `RouterApp` (ASGI
  adapter, #244) copied from pytheum-stream `api/routes.py` + `api/asgi.py`.
  Stage 3b will delete the originals from stream.
- `pytheum.registry`: `RouterRegistry` with `RouteSpec` (summary/tags/params)
  and replace-on-duplicate semantics — last registration for a pattern wins
  (load-bearing for pytheum-pit route overrides).
- `pytheum.config`: `ServeConfig` pydantic-settings model covering all 16
  serve-boundary environment variables from the 2026-06-13 Stage-0 reference.
- `datasets/MANIFEST.json`: artifact registry skeleton with schema documentation.
- `scripts/gen_checksums.py`: generate SHA-256 `.sha256` sidecars for
  `datasets/*.gz` artifacts.
- `scripts/verify_checksums.py`: verify `.sha256` sidecars; used as a CI gate.
- `scripts/gen_openapi.py`: generate `openapi.yaml` from the stub registry;
  `--check` mode diffs against the committed file for CI drift detection.
- `openapi.yaml`: committed skeleton generated from the serve-side route
  inventory (Stage-0 S-boundary routes, 14 paths).
- CI: lint (ruff), mypy --strict, pytest matrix (3.11/3.12, cov ≥ 80%),
  openapi-check, checksum-verify; publish job stubbed behind `refs/tags/v*`.
- `.importlinter` contract: `pytheum` must not import `pytheum_pit`.
- `.gitattributes`: Git LFS rule for `datasets/*.gz`.
