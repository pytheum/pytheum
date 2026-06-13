"""pytheum — public serve-side library.

Routing, registry, config, and dataset tooling for the Pytheum API.
"""
from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version

try:
    __version__: str = version("pytheum")
except PackageNotFoundError:  # pragma: no cover
    __version__ = "0.0.0+uninstalled"

__all__ = ["__version__"]
