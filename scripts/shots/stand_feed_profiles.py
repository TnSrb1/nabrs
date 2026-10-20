#!/usr/bin/env python3
"""The cabinet stand with SEVERAL templates for /app/feed?sort=profile (the “By template” groups).

An add-on over stand_feed_search.py: three profiles of the tenant, 12 fake matches,
distributed across them; with sort=profile the selection is sorted as in the SQL
(the template name, then the fresh ones on top). Not about production data — about the layout of the groups.

    python3 scripts/shots/stand_feed_profiles.py 8771

Prints the cookie tb_session for shot.sh --cookie.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import stand_feed_search as base  # noqa: E402

_base_items = base._items

PROFILES = [
    SimpleNamespace(id="p-fuel", name="Гориво", is_active=True, enabled_source_keys=[]),
    SimpleNamespace(id="p-it", name="ИТ набавке", is_active=True, enabled_source_keys=[]),
    SimpleNamespace(id="p-car", name="Возила и сервис", is_active=True, enabled_source_keys=[]),
]
_BY_ID = {p.id: p for p in PROFILES}


def _items():
    out = []
    for i, item in enumerate(_base_items()):
        p = PROFILES[i % len(PROFILES)]
        out.append(
            base.feed_service.FeedItem(
                **{**item.__dict__, "profile_id": p.id, "profile_name": p.name}
            )
        )
    return out


async def _listing(db, tid, pf, *, page=1, page_size=20):
    items = [i for i in _items() if (pf.q or "").lower() in i.title.lower()]
    if pf.profile_id:
        items = [i for i in items if i.profile_id == pf.profile_id]
    if pf.sort == "profile":
        items.sort(key=lambda i: (i.profile_name, -i.sent_at.timestamp()))
    start = (page - 1) * page_size
    return items[start : start + page_size], len(items), False


async def _profiles(db, tid):
    return PROFILES


if __name__ == "__main__":
    # base.main() sets list_profiles=[] itself — we substitute directly before
    # the start of the server, not before calling main().
    _run = base.uvicorn.run

    def _run_with_profiles(*args, **kwargs):
        base.routes_profiles.svc.list_profiles = _profiles
        _run(*args, **kwargs)

    base.uvicorn.run = _run_with_profiles
    base._items = _items
    base._listing = _listing
    base.main()
