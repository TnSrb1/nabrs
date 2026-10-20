#!/usr/bin/env python3
"""A live cabinet stand (uvicorn, a fake DB) for the browser check of the feed search and
the dashboard teaser: /app/feed filters 12 fake tenders by ?q=, /app — the teaser of
N rows (env TEASER_N, by default as in the code) with the sorting ?teaser=.

    TEASER_N=5 python3 scripts/shots/stand_feed_search.py 8767
    node feed_search_e2e.js http://127.0.0.1:8767   # see feed_search_e2e.js

Not about the production figures: it checks the JS of the search reset and the layout, not the data.
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import uvicorn  # noqa: E402

from src.api import deps, security  # noqa: E402
from src.api.cabinet import feed_service, routes_profiles  # noqa: E402
from src.api.config import get_cabinet_settings  # noqa: E402
from src.api.main import app  # noqa: E402
from tests.api.conftest import FakeSession  # noqa: E402

# locale — from the signed session (deps.get_optional_session does not read the cookie in the stand).
SESS = {"uid": "u-1", "tid": "t-1", "locale": os.getenv("STAND_LOCALE", "sr")}
NOW = datetime.now(timezone.utc)
TITLES = [f"Набавка {w}" for w in ("рачунара", "хартије", "гориво", "возила")] * 3


def _items():
    out = []
    for i, title in enumerate(TITLES):
        out.append(
            feed_service.FeedItem(
                tender_id=f"t{i:02d}",
                title=f"{title} {i}",
                customer_name="Општина Нови Сад",
                # FEED_EXPIRED=1 — even rows with an expired deadline (the «deadline passed» badge in the archive).
                deadline_at=(
                    NOW - timedelta(days=1 + i)
                    if os.getenv("FEED_EXPIRED") and i % 2 == 0
                    else NOW + timedelta(days=30 - 2 * i)
                ),
                tender_url="https://portal.rs/t/1",
                profile_id="pppp",
                profile_name="ИТ набавке",
                ai_level=None,
                sent_at=NOW - timedelta(hours=i),
                ai_actions=feed_service.AiActions(
                    delivered_analysis_level=None,
                    has_bid_verdict=False,
                    has_checklist=False,
                    has_draft=False,
                ),
            )
        )
    return out


async def _listing(db, tid, pf, *, page=1, page_size=20):
    items = [i for i in _items() if (pf.q or "").lower() in i.title.lower()]
    return items, len(items), False


async def _teaser(db, tid, limit=10, sort="date"):
    items = _items()
    if sort == "deadline":
        items.sort(key=lambda i: i.deadline_at)
    return items[: int(os.getenv("TEASER_N", limit))]


def _async(v):
    async def f(*a, **k):
        return v

    return f


def main() -> None:
    app.dependency_overrides[deps.get_session_factory] = lambda: FakeSession
    app.dependency_overrides[deps.get_optional_session] = lambda: SESS
    feed_service.get_feed_listing = _listing
    feed_service.get_feed_items = _teaser
    feed_service.get_region_hidden_notice = _async(None)
    routes_profiles.svc.list_profiles = _async([])
    routes_profiles.svc.downgrade_notice = _async([])
    routes_profiles.channel_svc.has_active_telegram = _async(True)
    routes_profiles.channel_svc.delivery_channel_status = _async(
        {
            "has_live_channel": True,
            "live_channels": ["email"],
            "gaps": [],
            "channelless": [],
        }
    )
    # STAND_LOCALE — the interface language of the session (sr by default; en — the acceptance of the archive, owner 13.10).
    cookie = security.sign_session(
        "u-1",
        "t-1",
        get_cabinet_settings().cabinet_secret_key,
        locale=os.getenv("STAND_LOCALE", "sr"),
    )
    print("tb_session=" + cookie, flush=True)
    uvicorn.run(app, host="127.0.0.1", port=int(sys.argv[1]), log_level="warning")


if __name__ == "__main__":
    main()
