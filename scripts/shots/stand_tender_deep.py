#!/usr/bin/env python3
"""The static stand of the tender card with the deep button (/app/tenders/{id}) for the measurement.

It renders the page with the same application on a fake DB with the mocked services (as
tests/api/test_routes_tender_ai.py) and puts the HTML + ``src/api/static`` into the stand
directory.

    python3 scripts/shots/stand_tender_deep.py /tmp/tdstand [ru|sr|en] [button|soft|exhausted|done]
    (cd /tmp/tdstand && python3 -m http.server 8767 --bind 127.0.0.1)
"""

from __future__ import annotations

import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path.cwd()
sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient  # noqa: E402

from src.ai.usage import LevelUsage  # noqa: E402
from src.api import deps  # noqa: E402
from src.api.cabinet import ai_limits_service, routes_tender_ai  # noqa: E402
from src.api.cabinet.tender_ai_service import TenderAnalyses  # noqa: E402
from src.api.main import app  # noqa: E402
from tests.api.conftest import FakeSession  # noqa: E402

_TENDER = "22222222-2222-2222-2222-222222222222"
_PROFILE = "33333333-3333-3333-3333-333333333333"


def _usage(used: int) -> LevelUsage:
    return LevelUsage(
        level="deep",
        used=used,
        limit=10,
        state="exhausted" if used >= 10 else "ok",
        scope="month",
    )


def main() -> int:
    out = Path(sys.argv[1] if len(sys.argv) > 1 else "/tmp/tdstand")
    lang = sys.argv[2] if len(sys.argv) > 2 else "ru"
    mode = sys.argv[3] if len(sys.argv) > 3 else "button"
    out.mkdir(parents=True, exist_ok=True)
    outputs: dict = {}
    if mode == "done":
        outputs["deep"] = {
            "summary": "Набавка рачунарске опреме.\nРок кратак.",
            "match_score": 4,
            "red_flags": ["кратак рок"],
            "work_breakdown": ["Испорука", "Инсталација"],
        }
    data = TenderAnalyses(
        tender_id=_TENDER,
        profile_id=_PROFILE,
        profile_name="ИТ набавке",
        title="Набавка рачунара",
        customer_name="Општина Нови Сад",
        deadline_at=datetime(2026, 11, 15, 12, 0, tzinfo=timezone.utc),
        source_url="https://portal.rs/t/1",
        outputs=outputs,
    )
    used = {"exhausted": 10, "soft": 8}.get(mode, 2)
    limits = ai_limits_service.AiLimitsView(
        rows=(_usage(used),), period="2026-10", tier="pro", status="active"
    )

    async def _data(*a, **k):
        return data

    async def _limits(*a, **k):
        return limits

    routes_tender_ai.svc.get_tender_analyses = _data
    ai_limits_service.get_ai_limits = _limits
    app.dependency_overrides[deps.get_session_factory] = lambda: (
        lambda: FakeSession(user_locale=lang)
    )
    app.dependency_overrides[deps.get_optional_session] = lambda: {
        "uid": "u-1",
        "tid": "t-1",
    }
    resp = TestClient(app, follow_redirects=False).get(
        f"/app/tenders/{_TENDER}?profile_id={_PROFILE}"
    )
    resp.raise_for_status()
    (out / "tender.html").write_text(resp.text, encoding="utf-8")
    shutil.copytree(ROOT / "src/api/static", out / "static", dirs_exist_ok=True)
    print(f"tender.html → {resp.status_code} ({lang}, {mode})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
