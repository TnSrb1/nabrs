#!/usr/bin/env python3
"""A live stand (uvicorn, a fake DB) for the acceptance of “To the calendar” (.ics).

    python3 scripts/shots/stand_ics.py <port>

It serves over HTTP: the card ``/tender/<id>`` (with the button, the deadline in the future),
``/tender/<id>.ics``, ``/app/my`` (logged in, the “All deadlines (.ics)” button),
``/app/tenders/calendar.ics``. The session is substituted only on ``/app/*`` — the card
stays anonymous, as for a search engine and a guest. The DB is FakeSession, as in the tests.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

ROOT = Path.cwd()
sys.path.insert(0, str(ROOT))

import uvicorn  # noqa: E402
from fastapi import Request  # noqa: E402

from src.api import deps  # noqa: E402
from src.api.cabinet import workflow_service as svc  # noqa: E402
from src.api.main import app  # noqa: E402
from tests.api.conftest import FakeResult, FakeSession  # noqa: E402
from tests.api.test_tender_page import _tender as _page_tender  # noqa: E402

TID = "11111111-1111-4111-8111-111111111111"


def _session_factory():
    deadline = datetime.now(timezone.utc) + timedelta(days=10)
    tender = _page_tender(
        id=TID,
        title="Nabavka opreme, sto i stolica",
        customer_name="Opština Novi Sad",
        deadline_at=deadline,
    )
    return lambda: FakeSession(results=[FakeResult(items=[tender])])


def _session_for(request: Request):
    # The cabinet (/app/*) — logged in; the public card — a guest.
    if request.url.path.startswith("/app"):
        return {"uid": "u-1", "tid": "t-1"}
    return None


async def _my_rows(db, tenant_id, now):
    return [
        SimpleNamespace(
            tender_id=TID,
            title="Nabavka opreme, sto i stolica",
            customer_name="Opština Novi Sad",
            deadline_at=datetime.now(timezone.utc) + timedelta(days=10),
        )
    ]


async def _my_page(db, tenant_id):
    return svc.MyTenders(
        folders=[],
        funnel=[
            svc.MyTenderRow(
                TID,
                "Nabavka opreme, sto i stolica",
                None,
                None,
                None,
                status="watching",
            )
        ],
    )


def main() -> int:
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8771
    app.dependency_overrides[deps.get_session_factory] = lambda: _session_factory()
    app.dependency_overrides[deps.get_optional_session] = _session_for
    svc.get_calendar_deadlines = _my_rows
    svc.get_my_tenders = _my_page
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
