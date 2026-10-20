#!/usr/bin/env python3
"""The /app/feed stand with the matches cut by the daily limit (the “not sent” mark).

An add-on over stand_feed_profiles.py: every third match is ``capped=True``.
Not about production data — about the layout of the mark (owner 16.10.2026).

    python3 scripts/shots/stand_feed_capped.py 8772

Prints the cookie tb_session for shot.sh --cookie.
"""

from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import stand_feed_profiles as prof  # noqa: E402

_prof_items = prof._items


def _items():
    return [
        prof.base.feed_service.FeedItem(**{**item.__dict__, "capped": i % 3 == 0})
        for i, item in enumerate(_prof_items())
    ]


if __name__ == "__main__":
    _run = prof.base.uvicorn.run

    def _run_with_profiles(*args, **kwargs):
        prof.base.routes_profiles.svc.list_profiles = prof._profiles
        _run(*args, **kwargs)

    prof.base.uvicorn.run = _run_with_profiles
    prof._items = _items
    prof.base._items = _items
    prof.base._listing = prof._listing
    prof.base.main()
