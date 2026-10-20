#!/usr/bin/env python3
"""The stand of the home page `/` (the landing): a live uvicorn + a fake DB, but with the FACTS of the showcase —
the block «Šta vas zanima?» and the line «K izvora · M zemalja» are drawn from the data.

    python3 scripts/shots/stand_landing.py 8771
    # open http://127.0.0.1:8771/?lang=sr (anonymously); without a cookie — an anonymous user

An add-on over stand_nav.py: it substitutes the facts (geo_cache.get_landing_facts) and the view of
the sources (routes_public.get_sources_view) with the fixture of six enabled platforms (RS/BA/ME/MK — as on production on 17.10.2026). The fixture numbers are not production, but a check
of the layout and the substitution; the production figures are read from the DB separately.
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import stand_nav  # noqa: E402

from src.api.public import geo_cache, routes_public  # noqa: E402
from src.api.public.sources_view import SourceItem, SourcesView  # noqa: E402

#: The six enabled platforms of the stand: the name and the countries (the union — RS/BA/ME/MK).
STAND_SOURCES = [
    ("Portal javnih nabavki", ("RS",)),
    ("Uprava za javne nabavke", ("RS",)),
    ("Opštine (čl. 27)", ("RS",)),
    ("TED", ("BA", "ME", "MK", "RS")),
    ("e-Nabavke BiH", ("BA",)),
    ("CEJN", ("ME",)),
]


def _patch() -> None:
    async def facts(_db):
        return {
            "open_total": 1698,
            "updated_at": datetime.now(timezone.utc),
            "example": None,
            "sources_count": len(STAND_SOURCES),
            "countries_count": len({c for _, cs in STAND_SOURCES for c in cs}),
        }

    async def view(_db):
        return SourcesView(tuple(SourceItem(n, cs) for n, cs in STAND_SOURCES))

    geo_cache.get_landing_facts = facts
    routes_public.get_sources_view = view


def main() -> None:
    real_run = stand_nav.uvicorn.run

    def run(*a, **k):
        _patch()
        real_run(*a, **k)

    stand_nav.uvicorn.run = run
    stand_nav.main()


if __name__ == "__main__":
    main()
