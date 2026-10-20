#!/usr/bin/env python3
"""The dashboard stand /app with the warning “a template exists, no channel”.

    DASH_CASE=b python3 scripts/shots/stand_dash.py 8771 active
    # DASH_CASE=a (default) — no live channel (tenders are not sent);
    # DASH_CASE=b — a channel exists, the template has none selected.
    # DASH_CASE=flow — a tenant with a FLOW (a live channel, 6 templates, ~40 deadlines in
    #   30 days, 5 deliveries, 6 tenders in progress): the measure of the length of the overview screen;
    # DASH_CASE=empty — an empty tenant (no templates, no flow).
    # DASH_CASE=newtpl — one fresh template without deliveries: the line “already open N”
    #   (the preview is a stub 8; on production N is counted by preview_counts).
    # argv[2] — the access state for stand_nav (trial/active/free/frozen), NOT the case: before
    # the case was read from argv[2] and overwrote the access → KeyError in _set_whoami.
    # prints hub_tb_session=… (signed, locale=sr) for shot.sh --cookie

The dashboard data are the same substitutions as in tests/api/test_cabinet_i18n.py::_stub_dashboard
(one truth about which services the dashboard calls); on top — the delivery state.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import stand_nav  # noqa: E402

from datetime import datetime, timedelta, timezone  # noqa: E402
from types import SimpleNamespace  # noqa: E402

from src.api import security  # noqa: E402
from src.api.cabinet import routes_profiles  # noqa: E402
from src.api.config import get_cabinet_settings  # noqa: E402
from src.api.cabinet import feed_service, stats_service, source_banner  # noqa: E402
from src.domain.sources import SourceInfo  # noqa: E402
from tests.api.test_cabinet_i18n import _async, _stub_dashboard  # noqa: E402


class _Patch:
    @staticmethod
    def setattr(obj, name, value):
        setattr(obj, name, value)


def _flow_case(empty: bool) -> None:
    """On top of the base substitutions — the data “with a flow” or of an empty tenant.

    The dates are computed from the REAL now: the base substitutions are tied to 20.07.2026
    and fall out of the calendar window.
    """
    rps = routes_profiles
    now = datetime.now(timezone.utc)
    ch = rps.channel_svc
    ch.dead_channels = _async([])
    ch.delivery_channel_status = _async(
        {
            "has_live_channel": not empty,
            "live_channels": [] if empty else ["email"],
            "gaps": [],
            "channelless": [],
        }
    )
    profiles = (
        []
        if empty
        else [
            SimpleNamespace(
                id=f"pppppppp-0000-0000-0000-00000000000{i}",
                name=n,
                is_active=i != 5,
                created_at=now - timedelta(days=30),
                # The “new source” banner: nis is not in any template; one has
                # “all tenders” (the match_all warning).
                enabled_source_keys=["jnportal"],
                source_opt_in={},
                match_all=i == 2,
            )
            for i, n in enumerate(
                [
                    "Gradnja",
                    "IT nabavka",
                    "Medicinska oprema",
                    "Nameštaj",
                    "Usluge čišćenja",
                    "Gorivo",
                ],
                1,
            )
        ]
    )
    rps.svc.list_profiles = _async(profiles)
    # The registry of platforms: jnportal long ago, nis was enabled an hour ago → the banner and “K of N”.
    source_banner.load_enabled_sources = _async(
        []
        if empty
        else [
            (
                SourceInfo("jnportal", "Portal javnih nabavki (UJN)", ("RS",), None),
                now - timedelta(days=60),
            ),
            (
                SourceInfo("nis", "NIS a.d. — otvoreni tenderi", ("RS",), None),
                now - timedelta(hours=1),
            ),
        ]
    )
    rps.svc.downgrade_notice = _async([])
    # profiles-stubs without topics otherwise get marked “will find nothing” and inflate the measurement
    rps.preview_service.profile_matches_anything = lambda p: True
    n_dl = 0 if empty else 40
    deadlines = [
        feed_service.HotDeadline(
            tender_id=f"t-{i:03d}",
            title=f"Nabavka robe i usluga broj {i}: radovi na održavanju objekata",
            customer_name="Opština Novi Sad",
            deadline_at=now + timedelta(days=0.5 + i * 0.7),
            tender_url="https://portal.rs/t/1",
            days_left=int(i * 0.7),
        )
        for i in range(n_dl)
    ]
    rps.feed_service.get_calendar_deadlines = _async(deadlines)
    rows = (
        []
        if empty
        else [
            SimpleNamespace(
                tender_id=f"m-{i}",
                title=f"Tender u radu {i}",
                customer_name=None,
                profile_id=profiles[0].id,
                deadline_at=now + timedelta(days=2 + i),
                tender_url="https://portal.rs/t/my",
                status=st,
            )
            for i, st in enumerate(
                ["active", "watching", "submitted", "active", "watching", "active"]
            )
        ]
    )
    rps.workflow_service.get_my_tenders = _async(SimpleNamespace(funnel=rows))
    # Number of the "upcoming deadlines" tile — COUNT in prod; the stand returns the same value,
    # what the route would compute from the same stub data (otherwise the tile = only "in progress").
    rps.feed_service.count_urgent_deadlines = _async(
        len(
            feed_service.urgent_events(
                feed_service.build_calendar_events(deadlines=deadlines, my_tenders=rows)
            )
        )
    )
    rps.feed_service.get_feed_items = _async(
        []
        if empty
        else [
            feed_service.FeedItem(
                tender_id=f"f-{i}",
                title=f"Isporuka opreme {i}",
                customer_name="JKP Beograd",
                # Deadlines of different buckets (expired / today / ≤2 / ≤7 / “until dd.mm”): the width of the badge
                # differs — on one bucket the right block has one width and the spread is not visible (the teaser measurement).
                deadline_at=now + timedelta(days=[-1, 0, 1.5, 5, 12][i]),
                tender_url="https://portal.rs/t/f",
                profile_id=profiles[0].id,
                profile_name="Gradnja",
                ai_level=None,
                sent_at=now - timedelta(hours=i + 1),
                ai_actions=None,
            )
            for i in range(5)
        ]
    )
    rps.feed_service.get_region_hidden_notice = _async(None)
    rps.stats_service.get_tenant_delivery_stats = _async(
        stats_service.TenantStats(
            total=0 if empty else 120,
            by_profile=(
                []
                if empty
                else [
                    stats_service.ProfileDelivery(
                        profile_id=p.id, profile_name=p.name, count=10 * i
                    )
                    for i, p in enumerate(profiles, 1)
                ]
            ),
        )
    )
    rps._flow_pulse = _async(0 if empty else 27)
    # Tržište (?vesti=trziste): sample figures instead of a DB query.
    ms = rps.market_service
    ms.get_shared = _async(
        {
            "day": "2026-10-13",
            "published": {
                "date": "2026-10-12",
                "total": 23,
                "by_country": {"RS": 14, "BA": 0, "ME": 0, "MK": 9},
            },
            "top": {
                "date": "2026-10-12",
                "items": [
                    {
                        "id": f"t-{i}",
                        "title": f"Nabavka velike vrednosti {i}",
                        "customer": "JKP Beograd",
                        "country": "RS",
                        "amount_eur": 9_000_000 / i,
                    }
                    for i in (1, 2, 3)
                ],
            },
            "awards": {
                "date": "2026-10-13",
                "count": 17,
                "known": 16,
                "total_rsd": 182998485.89,
                "top": {
                    "amount_rsd": 99590740.56,
                    "customer": "JKP Infostan tehnologije Beograd",
                    "tender_id": "t-1",
                },
            },
        }
    )
    ms.get_personal = _async(None if empty else {"new": 12, "expiring": 4})
    rps.trial_service.get_access_status = _async(
        rps.trial_service.AccessStatus(
            state="active", active=True, can_request=False, until="31.12.2026"
        )
    )
    rps.stats_service.get_days_left = _async(
        stats_service.AccessDays(label="20", days=20, until=now + timedelta(days=20))
    )


def _new_template_case() -> None:
    """The flow + one fresh template without deliveries: the line “already open N” (N = the stub 8)."""
    from tests.api.test_routes_profiles import _profile

    _flow_case(False)
    rps = routes_profiles
    now = datetime.now(timezone.utc)
    fresh = _profile(
        id="pppppppp-0000-0000-0000-000000000009",
        name="Čišćenje",
        created_at=now - timedelta(days=1),
        keywords_include=[{"all_of": ["ciscenje"]}],
    )
    rps.svc.list_profiles = _async([fresh])
    rps.stats_service.get_tenant_delivery_stats = _async(
        stats_service.TenantStats(total=0, by_profile=[])
    )
    rps.preview_service.preview_counts = _async([8])


def main() -> None:
    case = os.environ.get("DASH_CASE", "a")
    real_run = stand_nav.uvicorn.run

    def run(*a, **k):
        _stub_dashboard(_Patch)
        ch = routes_profiles.channel_svc
        ch.dead_channels = _async([])
        ch.delivery_channel_status = _async(
            {
                "has_live_channel": case == "b",
                "live_channels": ["email"] if case == "b" else [],
                "gaps": [],
                "channelless": (
                    [{"id": "pppppppp-0000-0000-0000-000000000001", "name": "Gradnja"}]
                    if case == "b"
                    else []
                ),
            }
        )
        if case in ("flow", "empty"):
            _flow_case(case == "empty")
        if case == "newtpl":
            _new_template_case()
        real_run(*a, **k)

    stand_nav.uvicorn.run = run
    cookie = security.sign_session(
        "u-1", "t-1", get_cabinet_settings().cabinet_secret_key, locale="sr"
    )
    print("hub_tb_session=" + cookie, flush=True)
    stand_nav.main()


if __name__ == "__main__":
    main()
