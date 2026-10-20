#!/usr/bin/env python3
"""A live stand of the screen “Template created” and of the edit with the block “Already open tenders”.

A fake DB, real templates: /profiles/p-1/created (after the creation) and
/profiles/p-1?updated=1 (after the edit). The mode of an empty list — env OPEN_NOW_N=0,
a check of “the honest empty text”. The stand figures are not production; the markup and the 390px layout of the block are checked.

    OPEN_NOW_N=15 python3 scripts/shots/stand_open_now.py 8771
    bash scripts/shots/shot.sh --url http://127.0.0.1:8771/profiles/p-1/created \
        --cookie tb_session=<printed by the stand> --full --out /out/open_now.png
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import uvicorn  # noqa: E402

from src.api import deps, security  # noqa: E402
from src.api.cabinet import preview_service, routes_profiles  # noqa: E402
from src.api.config import get_cabinet_settings  # noqa: E402
from src.api.main import app  # noqa: E402
from tests.api.conftest import FakeSession  # noqa: E402

SESS = {"uid": "u-1", "tid": "t-1"}
NOW = datetime.now(timezone.utc)
TITLES = [
    "Набавка канцеларијског материјала и потрошног рачунарског прибора за потребе управе",
    "Услуге одржавања видео надзора",
    "Набавка горива за службена возила",
]
PROFILE = SimpleNamespace(
    id="p-1",
    name="Video nadzor i kancelarija",
    is_active=True,
    ai_analysis_level="none",
    regions=[],
    delivery_channels=["email"],
    delivery_schedule="immediate",
    enabled_source_keys=["jnportal"],
    classifications={},
    min_amount_eur=None,
    max_amount_eur=None,
    min_days_until_deadline=None,
    include_no_amount=False,
    keywords_include=[{"all_of": ["video nadzor"]}],
    keywords_exclude=None,
    keyword_dict=False,
    debug_match=False,
    match_all=False,
    contract_types=[],
    customers_include=[],
    customers_exclude=[],
)


def _examples(n: int):
    out = []
    for i in range(n):
        title = TITLES[i % len(TITLES)] + f" бр. {i + 1}"
        out.append(
            preview_service.PreviewExample(
                id=f"t-{i:02d}",
                title_parts=[(title, False)],
                customer_name="Општина Нови Сад",
                deadline_at=NOW + timedelta(days=i + 1),
                archived=(i == 1),
            )
        )
    return out


async def _get_profile(*_a, **_kw):
    return PROFILE


async def _preview(db, payload, window_days=30, examples_limit=0):
    return preview_service.PreviewResult(
        count=3, unfound=[], examples=_examples(examples_limit)
    )


async def _open_now(db, payload, profile_id, limit=preview_service.OPEN_NOW_LIMIT):
    return _examples(int(os.getenv("OPEN_NOW_N", limit)))[:limit]


async def _no_downgrade(*_a, **_kw):
    return []


def main() -> None:
    app.dependency_overrides[deps.get_session_factory] = lambda: FakeSession
    app.dependency_overrides[deps.get_optional_session] = lambda: SESS
    routes_profiles.svc.get_profile = _get_profile
    routes_profiles.svc.downgrade_notice = _no_downgrade
    preview_service.preview_count = _preview
    preview_service.open_now_examples = _open_now
    cookie = security.sign_session(
        "u-1", "t-1", get_cabinet_settings().cabinet_secret_key
    )
    print("tb_session=" + cookie, flush=True)
    uvicorn.run(app, host="127.0.0.1", port=int(sys.argv[1]), log_level="warning")


if __name__ == "__main__":
    main()
