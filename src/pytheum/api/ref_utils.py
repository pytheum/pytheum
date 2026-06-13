"""Utilities for normalizing market refs — including URL extraction.

normalize_ref(ref) is the single entry-point applied at the top of every
endpoint that accepts a {ref} path segment or a `market_ref` tool argument.

Supported URL forms (HTTPS or HTTP, tolerant of trailing slashes and query
strings):

  kalshi.com/markets/<ticker>    → kalshi:<ticker>
  kalshi.com/events/<event>      → kalshi:<event>
  polymarket.com/event/<slug>    → polymarket:<slug>
  polymarket.com/market/<slug>   → polymarket:<slug>

Everything else passes through to the existing prefix-casing normalisation
(whitespace trim + venue-prefix case-fold).
"""
from __future__ import annotations

import re
from urllib.parse import urlparse

# Pattern: /markets/<ticker>  or  /events/<ticker>
_KALSHI_PATH_RE = re.compile(
    r"/(?:markets|events)/([A-Za-z0-9_\-.]+)",
    re.IGNORECASE,
)

# Pattern: /event/<slug>  or  /market/<slug>  (Polymarket uses both)
_PM_PATH_RE = re.compile(
    r"/(?:event|market)/([A-Za-z0-9_\-.]+)",
    re.IGNORECASE,
)


def _extract_from_url(ref: str) -> str | None:
    """Try to extract a venue-prefixed ref from a prediction-market URL.

    Returns None when the URL doesn't match a known pattern so the caller
    can fall through to the default normalisation path.
    """
    try:
        parsed = urlparse(ref)
    except Exception:
        return None
    if not parsed.scheme:
        return None

    raw_host = (parsed.netloc or "").lower()
    host = raw_host[4:] if raw_host.startswith("www.") else raw_host
    path = parsed.path or ""

    if "kalshi.com" in host:
        m = _KALSHI_PATH_RE.search(path)
        if m:
            ticker = m.group(1).rstrip("/")
            if ticker:
                return f"kalshi:{ticker}"
        return None

    if "polymarket.com" in host:
        m = _PM_PATH_RE.search(path)
        if m:
            slug = m.group(1).rstrip("/")
            if slug:
                return f"polymarket:{slug}"
        return None

    return None


def normalize_ref(ref: str) -> str:
    """Normalize a market ref to a canonical venue-prefixed form.

    Processing order
    ----------------
    1. Strip surrounding whitespace.
    2. If the result looks like a URL (http:// or https://) try to extract
       a venue-prefixed id from the path — kalshi.com/markets/<t> becomes
       'kalshi:<t>'; polymarket.com/event/<slug> becomes 'polymarket:<slug>'.
       Unrecognized URLs are returned as-is (the lookup will find nothing and
       the caller can 404 gracefully).
    3. Case-fold the venue prefix so 'Kalshi:TICKER' becomes 'kalshi:TICKER'
       and 'POLYMARKET:slug' becomes 'polymarket:slug'.
    """
    if not isinstance(ref, str):
        return ref

    ref = ref.strip()
    if not ref:
        return ref

    low = ref.lower()
    if low.startswith("http://") or low.startswith("https://"):
        extracted = _extract_from_url(ref)
        if extracted is not None:
            return extracted
        # Unrecognized URL — return as-is; the endpoint will 404 cleanly.
        return ref

    # Case-fold the venue prefix only.
    head, sep, body = ref.partition(":")
    if sep and head.strip().lower() in ("kalshi", "polymarket", "manifold"):
        return f"{head.strip().lower()}:{body.strip()}"

    return ref
