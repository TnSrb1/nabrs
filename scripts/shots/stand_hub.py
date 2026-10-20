#!/usr/bin/env python3
"""The stand of the hub «Moj kabinet» (/cabinet/account): a live uvicorn + a fake DB, but with a
FLOW of data (3 templates, Telegram linked, Email awaiting confirmation, the subscription,
AI limits) — the overview screen is measured by length on an account with data, not on an empty one.

    python3 scripts/shots/stand_hub.py 8769 [trial|active|free|frozen]
    # prints the cookie tb_session=… (signed, locale=sr) for shot.sh --cookie

An add-on over stand_nav.py (the header/whoami/nav_access by argv[2]); the hub services are substituted
the same way as in tests/api/test_routes_account.py::_setup.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import stand_nav  # noqa: E402

from src.api import security  # noqa: E402
from src.api.cabinet import routes_account, routes_subscription  # noqa: E402
from src.api.cabinet.stats_service import AccessDays  # noqa: E402
from src.api.cabinet.trial_service import AccessStatus  # noqa: E402
from src.api.config import get_cabinet_settings  # noqa: E402


def _async(v):
    async def f(*a, **k):
        return v

    return f


def _profile(i, name, active=True):
    return SimpleNamespace(
        id=f"p-{i}",
        name=name,
        is_active=active,
        match_all=False,
        keywords_include=[{"all_of": ["gradnja", "putevi"]}],
        regions=["RS-00"],
    )


def _patch() -> None:
    state = sys.argv[2] if len(sys.argv) > 2 else "active"
    now = datetime.now(timezone.utc)
    tier, status, days = {
        "trial": ("lite", "trial", 21),
        "active": ("pro", "active", 12),
        "free": ("lite", "active", None),
        "frozen": ("pro", "active", -3),
    }[state]
    until = None if days is None else now + timedelta(days=days)
    sub = (
        SimpleNamespace(
            provider="paddle",
            status="active",
            plan_key="pro",
            current_period_end=until,
        )
        if state == "active"
        else None
    )
    s = routes_subscription
    s.svc.get_tenant_tier = _async((tier, status))
    s.svc.get_subscription = _async(sub)
    s.svc.get_last_decision = _async(None)
    s.stats_service.get_days_left = _async(AccessDays(label="", days=days, until=until))
    s.ai_limits_service.get_ai_limits = _async(
        SimpleNamespace(
            period="oktobar",
            rows=[
                SimpleNamespace(
                    level="standard", scope="plan", state="ok", limit=400, used=17
                ),
                SimpleNamespace(
                    level="deep", scope="plan", state="ok", limit=20, used=3
                ),
            ],
        )
    )
    a = routes_account
    a.trial_service.get_access_status = _async(
        AccessStatus(
            state="expired" if state == "frozen" else "active",
            active=state != "frozen",
            can_request=state == "frozen",
            until=None,
        )
    )
    a.svc.list_profiles = _async(
        [
            _profile(1, "Gradnja puteva"),
            _profile(2, "IT oprema"),
            _profile(3, "Nameštaj", active=False),
        ]
    )
    a.channel_svc.delivery_warning = _async(None)
    a.channel_svc.telegram_channel_state = _async(SimpleNamespace(is_live=True))
    a.channel_svc.email_channel_state = _async(
        SimpleNamespace(
            is_live=False, status="pending", external_id="client@example.rs"
        )
    )


def main() -> None:
    # stand_nav.main() substitutes list_profiles with an empty list by itself — our substitutions
    # are applied AFTER it, before the start of the server.
    real_run = stand_nav.uvicorn.run

    def run(*a, **k):
        _patch()
        real_run(*a, **k)

    stand_nav.uvicorn.run = run
    cookie = security.sign_session(
        "u-1", "t-1", get_cabinet_settings().cabinet_secret_key, locale="sr"
    )
    # stand_nav.main() prints its own cookie (without locale) — we need sr; we print
    # ours as the LAST line before starting the server is not possible (main blocks),
    # therefore we print it before the call.
    print("hub_tb_session=" + cookie, flush=True)
    stand_nav.main()


if __name__ == "__main__":
    main()
