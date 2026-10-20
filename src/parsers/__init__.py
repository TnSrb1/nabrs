"""Source adapters package (ADR-021).

Importing the package does NOT register the adapters (SRC-0a): the registry is filled by the explicit
:func:`src.parsers.registry.load_all_adapters`, and whether to load a source is
decided by the ``sources`` table. The package only re-exports the contract.
"""

from __future__ import annotations

from src.parsers.base import (
    NormalizedTender,
    RawTender,
    SourceAdapter,
    get_source,
    register_source,
    registered_sources,
)

__all__ = [
    "SourceAdapter",
    "RawTender",
    "NormalizedTender",
    "register_source",
    "get_source",
    "registered_sources",
]
