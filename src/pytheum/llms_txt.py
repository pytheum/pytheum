"""GET /llms.txt — agent-readable plain-text manifest of the Pytheum API.

This is a skeleton constant shipped with the pytheum library.  The full
endpoint list is populated in Stage 6 of the restructure once all handlers
have migrated.  The RouterApp in routing.py serves this at /llms.txt.
"""
from __future__ import annotations

LLMS_TXT: str = """\
# Pytheum API — agent manifest

Pytheum is a real-time prediction market intelligence API providing
verified cross-venue equivalence data (Kalshi<->Polymarket), live order-
book quotes, news/social context, and trader analytics.

Base URL:      https://api.pytheum.com
MCP connector: https://api.pytheum.com/mcp

(Full endpoint list populated in Stage 6 of the restructure.)
"""
