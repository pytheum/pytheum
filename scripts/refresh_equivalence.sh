#!/usr/bin/env bash
# Periodic equivalence maintenance (systemd timer pytheum-equivalence-refresh,
# every 6h). DB + public APIs only — NO GitHub dependency: the matcher repo is
# private and the box has no creds, so dataset reloads stay a laptop
# one-command when a new build ships:
#   python -m scripts.load_market_equivalence --in <dataset-all.jsonl.gz> \
#       --source-commit <sha>
# Everything here is idempotent and cheap when there's nothing new.
set -euo pipefail
cd "$(dirname "$0")/.."  # pytheum repo root (unit sets WorkingDirectory)
PY="${PYTHON:-python3}"  # unit points this at pit-stack venv

$PY -m scripts.resolve_polymarket_slugs          # resolve new kalshi-known slugs via Gamma
$PY -m scripts.sync_paired_polymarket            # fill newly-resolved missing poly rows
$PY -m scripts.sync_paired_polymarket --refresh  # re-pull quotes on supplemental rows
$PY -m scripts.map_pair_sides                    # side-map new winner-style pairs
$PY -m scripts.sweep_settled_markets             # flip stuck active rows to venue truth (#260)
