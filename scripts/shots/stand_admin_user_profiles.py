#!/usr/bin/env python3
"""Live stand (uvicorn, a fake DB) for the measurement of the card /admin/users/{uid} — the block “Templates”.

    python3 scripts/shots/stand_admin_user_profiles.py 8772
    # then: bash scripts/shots/shot.sh --url "http://127.0.0.1:8772/admin/users/<UID>“ --clip ”#profiles" ...

The operator is substituted by dependencies (as in tests/api/test_admin_user_profiles.py): the admin
session — without a cookie, production is not touched. The data are fixtures: 2 active, 1 disabled
subscriber template and 1 foreign one (another tenant, must not get into the block). Not production numbers.
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import uvicorn  # noqa: E402

from src.api import deps  # noqa: E402
from src.api.config import CabinetSettings  # noqa: E402
from src.api.main import app  # noqa: E402
from tests.api.conftest import (  # noqa: E402
    FakeResult,
    FakeSession,
    make_session_factory,
)

OPERATOR = "admin@example.com"
ADMIN_SESSION = {
    "uid": "aaaa0000-0000-0000-0000-000000000001",
    "tid": "bbbb0000-0000-0000-0000-000000000002",
}
UID = "eeee0000-0000-0000-0000-000000000001"
TID = "ffff0000-0000-0000-0000-000000000002"
OTHER_TID = "ffff0000-0000-0000-0000-000000000003"
NOW = datetime(2026, 10, 16, 12, 0, 0, tzinfo=timezone.utc)


def _user():
    return SimpleNamespace(
        id=UID,
        email="vlada@obrazovanje.rs",
        created_at=NOW,
        tenant_id=TID,
        tier="pro",
        access_until=None,
        profile_count=3,
        deleted_at=None,
    )


def _profile(name, tenant_id, is_active, match_7d, created_at, delivery_channels):
    return SimpleNamespace(
        id=f"pid-{name}",
        name=name,
        tenant_id=tenant_id,
        is_active=is_active,
        owner_email="vlada@obrazovanje.rs",
        match_7d=match_7d,
        created_at=created_at,
        delivery_channels=delivery_channels,
    )


PROFILES = [
    _profile("Građevinski radovi, Beograd", TID, True, 14, NOW, ["telegram"]),
    _profile("IT oprema i licence", TID, True, 3, NOW, []),
    _profile("Stari šablon (isključen)", TID, False, 0, NOW, ["telegram"]),
    _profile("Чужой тенант — не виден", OTHER_TID, True, 9, NOW, []),
]

USER_CHANNELS = [
    SimpleNamespace(channel="telegram", status="active", linked_at=NOW),
]


def _results():
    return [
        FakeResult(items=[SimpleNamespace(email=OPERATOR)]),  # require_admin
        FakeResult(items=[_user()]),  # get_user
        FakeResult(scalar=42),  # get_user_delivered_count
        FakeResult(items=list(PROFILES)),  # app_admin_list_profiles()
        FakeResult(items=list(USER_CHANNELS)),  # app_admin_user_channels()
    ]


def _settings() -> CabinetSettings:
    return CabinetSettings(CABINET_ADMIN_EMAILS=OPERATOR)


def main() -> None:
    # A fresh queue for EVERY request (FakeSession is one-off).
    app.dependency_overrides[deps.get_session_factory] = lambda: make_session_factory(
        FakeSession(results=_results())
    )
    app.dependency_overrides[deps.get_optional_session] = lambda: ADMIN_SESSION
    app.dependency_overrides[deps.get_settings] = _settings
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8772
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")


if __name__ == "__main__":
    main()
