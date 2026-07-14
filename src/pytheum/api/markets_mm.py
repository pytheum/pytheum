"""GET /v1/markets/{ref}/mm_reference — market-maker cross-venue reference layer.

Composes the two already-assembled endpoints — /equivalents (oriented cross-venue
prices + book) and /rules (resolution text + resolution_at + method/confidence) — and
runs the market-maker analytics (``pytheum.mm``): a consolidated reference fair value
``p_hat``, the ``basis``, a ``fungibility`` verdict (is the pair safe to treat as ONE
instrument / a hedge — with a settlement-divergence veto detected from the two legs'
rules text), and the Avellaneda-Stoikov risk inputs (Bernoulli terminal variance + T).

Reuses the two handlers verbatim (same dao/equivalence), so all orientation and
graceful-degradation logic lives in ONE place. The data layer, not the edge.
"""
from __future__ import annotations

from typing import Any

from pytheum.api.markets_equivalents import handle_market_equivalents
from pytheum.api.markets_rules import handle_market_rules
from pytheum.mm import assemble_mm_reference


async def handle_market_mm_reference(
    ref: str,
    query: dict[str, str],
    *,
    dao: Any,
    equivalence: Any = None,
) -> tuple[int, dict[str, Any]]:
    """GET /v1/markets/{ref}/mm_reference handler.

    ``equivalence`` accepts an EquivalenceIndex (or duck-typed equivalent); defaults to
    the module-level singleton via the underlying handlers.
    """
    _eq_status, equ = await handle_market_equivalents(
        ref, query, dao=dao, equivalence=equivalence)
    _ru_status, rul = await handle_market_rules(
        ref, query, dao=dao, equivalence=equivalence)
    return 200, assemble_mm_reference(ref, equ, rul)
