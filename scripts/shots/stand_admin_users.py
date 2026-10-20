#!/usr/bin/env python3
"""Live stand (uvicorn, a fake DB) for the measurement of /admin/users — sorting by the columns.

    python3 scripts/shots/stand_admin_users.py 8771
    # then: bash scripts/shots/shot.sh --url "http://127.0.0.1:8771/admin/users?sort=access_until&dir=asc" ...

The operator is substituted by dependencies (as in tests/api/test_admin.py): the admin session —
without a cookie, production is not touched. The data — 6 fixture rows (different plans, NULL plan
and access), not production numbers. It checks the layout of the headers and the order of the rows.
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
from tests.api.conftest import (
    FakeResult,
    FakeSession,
    make_session_factory,
)  # noqa: E402

OPERATOR = "admin@example.com"
ADMIN_SESSION = {
    "uid": "aaaa0000-0000-0000-0000-000000000001",
    "tid": "bbbb0000-0000-0000-0000-000000000002",
}


def _d(y, m, d):
    return datetime(y, m, d, tzinfo=timezone.utc)


def _row(i, email, tier, access_until, profiles, created):
    return SimpleNamespace(
        id=f"cccc0000-0000-0000-0000-{i:012d}",
        email=email,
        created_at=created,
        tenant_id=f"dddd0000-0000-0000-0000-{i:012d}",
        tier=tier,
        access_until=access_until,
        profile_count=profiles,
        deleted_at=None,
    )


# The entry order — created_at DESC (as in SECURITY DEFINER).
ROWS = [
    _row(6, "vlada@obrazovanje.rs", "enterprise", _d(2026, 7, 1), 3, _d(2026, 9, 20)),
    _row(5, "dom-zdravlja@nb.rs", None, None, 0, _d(2026, 9, 2)),
    _row(4, "opstina@ns.rs", "pro", _d(2026, 5, 1), 2, _d(2026, 7, 15)),
    _row(3, "skola@beograd.rs", "lite", _d(2026, 9, 1), 1, _d(2026, 6, 3)),
    _row(2, "biblioteka@kg.rs", "starter", _d(2026, 11, 2), 1, _d(2026, 5, 11)),
    _row(1, "centar@nis.rs", "pro", _d(2026, 2, 28), 0, _d(2026, 1, 19)),
]


def _results():
    return [
        FakeResult(items=[SimpleNamespace(email=OPERATOR)]),  # require_admin
        FakeResult(items=list(ROWS)),  # list_users (window or SQL)
        FakeResult(
            items=[SimpleNamespace(total_all_time=0, total_month=0, total_day=0)]
        ),
        FakeResult(scalar=len(ROWS)),  # users total
    ]


def _settings() -> CabinetSettings:
    return CabinetSettings(CABINET_ADMIN_EMAILS=OPERATOR)


def main() -> None:
    # A fresh queue for EVERY request (FakeSession is one-off): otherwise the second snapshot
    # goes into an empty queue.
    app.dependency_overrides[deps.get_session_factory] = lambda: make_session_factory(
        FakeSession(results=_results())
    )
    app.dependency_overrides[deps.get_optional_session] = lambda: ADMIN_SESSION
    app.dependency_overrides[deps.get_settings] = _settings
    uvicorn.run(app, host="127.0.0.1", port=int(sys.argv[1]), log_level="warning")


if __name__ == "__main__":
    main()
