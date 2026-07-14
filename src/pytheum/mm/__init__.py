"""Market-maker reference layer — the cross-venue fair-value + fungibility signal
a prediction-market MARKET MAKER plugs into its OWN quoting model.

Pytheum is the data layer, not the edge: we fuse both venues' live prices into one
reference probability, decide whether the two contracts are actually FUNGIBLE (safe
to treat as one instrument / a hedge), and hand the maker the A-S/GL-FT risk inputs.
The maker keeps its quoting, sizing, and execution. See ``reference`` for the
analytics and ``resolution_fields`` for the settlement-divergence detector.
"""
from __future__ import annotations

from pytheum.mm.compose import assemble_mm_reference
from pytheum.mm.reference import (
    Fungibility,
    Leg,
    advise,
    as_reference_quote,
    fungibility,
    reference_fair_value,
    terminal_variance,
    time_to_resolution_years,
)
from pytheum.mm.resolution_fields import (
    ResolutionFields,
    divergence_from_text,
    extract_fields,
    settlement_divergence,
)

__all__ = [
    "Fungibility",
    "Leg",
    "ResolutionFields",
    "advise",
    "as_reference_quote",
    "assemble_mm_reference",
    "divergence_from_text",
    "extract_fields",
    "fungibility",
    "reference_fair_value",
    "settlement_divergence",
    "terminal_variance",
    "time_to_resolution_years",
]
