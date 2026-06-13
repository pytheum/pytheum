"""Shared pytest fixtures and sys.path setup for the pytheum test suite."""
from __future__ import annotations

import sys
from pathlib import Path

# Make the scripts/ directory importable so dataset-script tests can do
# ``import gen_checksums`` and ``import verify_checksums`` without installing.
_SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))
