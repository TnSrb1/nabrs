#!/usr/bin/env python3
"""A live stand (uvicorn, a fake DB) for the measurement of the navigation “All tenders”/“Sources”:
the cabinet menu, the public header, the tender card, an empty feed, /izvori.

    python3 scripts/shots/stand_nav.py 8768 [trial|active|free|frozen] [admin]
    # prints the cookie tb_session; without a cookie the public pages — as an anonymous user

The session is considered logged in only when the cookie tb_session is present (otherwise
the anonymous showcase). Not about the production figures: it checks the layout and the links.
"""

from __future__ import annotations

import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import uvicorn  # noqa: E402
from fastapi import Request  # noqa: E402

from src.api import admin_access, deps, repo, security  # noqa: E402
from src.api.nav_access import resolve_nav_access  # noqa: E402
from src.api.cabinet import feed_service, routes_profiles  # noqa: E402
from src.api.config import get_cabinet_settings  # noqa: E402
from src.api.main import app  # noqa: E402
from src.api.public import geo_cache, routes_public, routes_tender  # noqa: E402
from src.api.public.sources_page import SourceCard  # noqa: E402
from src.parsers.capabilities import capabilities_by_key  # noqa: E402
from tests.api.conftest import FakeSession  # noqa: E402

SESS = {"uid": "u-1", "tid": "t-1"}
TENDER = SimpleNamespace(
    id="t-1",
    title="Набавка рачунарске опреме за школе",
    customer_name="Општина Нови Сад",
    amount_eur=12345.0,
    is_amount_estimated=False,
    deadline_at=datetime.now(timezone.utc) + timedelta(days=20),
    status="active",
    regions=["RS-00"],
    procedure_type=None,
    description=None,
    classifications=[{"scheme": "CPV", "code": "30213000", "label": "Рачунари"}],
    source_url="https://portal.rs/t/1",
    source_key="jnportal",
    canonical_tender_id=None,
)


def _async(v):
    async def f(*a, **k):
        return v

    return f


async def _empty_listing(db, tid, pf, *, page=1, page_size=20):
    return [], 0, False


def _opt_session(request: Request):
    return SESS if request.cookies.get("tb_session") else None


def _set_whoami(request: Request) -> None:
    """As on production: for a logged-in user the header has the email + tenant line (+~38px)."""
    request.state.cabinet_whoami_email = "client@example.rs"
    request.state.cabinet_whoami_tenant_name = "ДОО Пример"
    # The access state for the badge/marks of the menu (argv[2]; by default active/pro).
    now = datetime.now(timezone.utc)
    state = sys.argv[2] if len(sys.argv) > 2 else "active"
    status, tier, until = {
        "trial": ("trial", "lite", now + timedelta(days=21)),
        "active": ("active", "pro", now + timedelta(days=12)),
        "free": ("active", "lite", None),
        "frozen": ("active", "pro", now - timedelta(days=3)),
    }[state]
    request.state.cabinet_nav_access = resolve_nav_access(status, tier, until, now)
    request.state.cabinet_access_frozen = state == "frozen"


async def _admin_flag(session, session_factory, settings):
    """The platform operator («Admin» item in the menu): argv[3] = admin."""
    return True


async def _whoami_dep(request: Request):
    _set_whoami(request)


async def _whoami_direct(request, session=None, session_factory=None):
    _set_whoami(request)


def main() -> None:
    # ‼️ Without whoami the header measurement lies by ~38px: _cabinet_nav.html renders
    # .site-header__identity inside the sticky <header> for every logged-in user.
    real_whoami = deps.resolve_cabinet_whoami
    app.dependency_overrides[real_whoami] = _whoami_dep
    deps.resolve_cabinet_whoami = (
        _whoami_direct  # resolve_public_authed_header is called directly
    )
    if len(sys.argv) > 3 and sys.argv[3] == "admin":
        # deps.resolve_platform_admin_flag is called directly (not via Depends), therefore
        # we substitute the operator resolve itself in admin_access.
        admin_access.is_platform_admin = _admin_flag
    app.dependency_overrides[deps.get_session_factory] = lambda: FakeSession
    app.dependency_overrides[deps.get_optional_session] = _opt_session
    geo_cache._cpv_cache = (
        time.monotonic() + 36000,
        {"30": {"total": 60, "open": 5, "open_sum_eur": None, "label": "Рачунари"}},
    )
    geo_cache._region_cache = (
        time.monotonic() + 36000,
        {"RS-00": {"total": 80, "open": 9, "open_sum_eur": None}},
    )
    geo_cache._sources_cache = (
        time.monotonic() + 36000,
        {"jnportal": "Portal javnih nabavki (UJN)"},
    )
    caps = capabilities_by_key()
    card = SourceCard(
        key="jnportal",
        name="Portal javnih nabavki (UJN)",
        country_codes=("RS",),
        url="https://jnportal.src-ujn.example.invalid",
        polling_interval_sec=1800,
        collecting_since=datetime(2026, 6, 3, tzinfo=timezone.utc),
        caps=caps.get("jnportal"),
    )
    routes_public.get_source_cards = _async((card,))
    repo.get_tender = _async(TENDER)
    repo.count_canonical_alts = _async(0)
    repo.get_tender_ai_analysis = _async(None)
    routes_tender.repo = repo
    feed_service.get_feed_listing = _empty_listing
    feed_service.get_feed_items = _async([])
    feed_service.get_region_hidden_notice = _async(None)
    routes_profiles.svc.list_profiles = _async([])
    routes_profiles.svc.downgrade_notice = _async([])
    routes_profiles.channel_svc.has_active_telegram = _async(True)
    routes_profiles.channel_svc.delivery_channel_status = _async(
        {"has_live_channel": True, "gaps": []}
    )
    cookie = security.sign_session(
        "u-1", "t-1", get_cabinet_settings().cabinet_secret_key
    )
    print("tb_session=" + cookie, flush=True)
    uvicorn.run(app, host="127.0.0.1", port=int(sys.argv[1]), log_level="warning")


if __name__ == "__main__":
    main()
