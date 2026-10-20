"""The platform checkbox rows — one set for the template form, “Composition” and “as is”.

The labels come from the ``sources`` registry (:func:`list_sources_for_selection`), the capability row is the adapter’s declaration (as on the showcase /izvori). The countries are CODES: the template takes the country label from the catalog (``country_XX``), not the code.

Moved out of ``routes_profiles``: the composition showcase (``routes_library``) cannot import ``routes_profiles`` (a reverse import would be a cycle).
"""

from __future__ import annotations

from typing import Any, Iterable

from src.domain.sources import list_sources_for_selection
from src.parsers.capabilities import capabilities_by_key, short_keys

__all__ = ["source_choice_rows"]


async def source_choice_rows(session: Any, selected: Iterable[str] = ()) -> list[dict]:
    """The enabled sources + those already selected (the disabled ones — with ``paused``)."""
    sources = await list_sources_for_selection(session, list(selected or ()))
    # The capability row under a source — the same adapter declaration as on the showcase
    # /izvori (src.parsers.capabilities); the cap_s_* keys — the COMMON catalog.
    caps = capabilities_by_key()
    return [
        {
            "key": s.source_key,
            "label": s.label,
            "countries": list(s.country_codes),
            "paused": not s.enabled,
            "caps_short": (
                short_keys(caps[s.source_key]) if s.source_key in caps else []
            ),
        }
        for s in sources
    ]
