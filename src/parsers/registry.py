"""Loading source adapters into the registry.

In the public snapshot the connectors to real sources are not published: the registry
contains ONE demo adapter ``example`` (a local file, no network).
Your own adapter is added with one line in :func:`load_all_adapters`; the contract —
:mod:`src.parsers.base`, the capabilities declaration — :mod:`src.parsers.capabilities`.
"""

from __future__ import annotations

from src.parsers.base import SourceAdapter, registered_sources


def load_all_adapters() -> dict[str, type[SourceAdapter]]:
    """Import the adapter modules and return a snapshot of the registry."""
    import src.parsers.example.adapter  # noqa: F401 — registration by @register_source

    return registered_sources()
