#!/usr/bin/env python3
"""The static stand of the template form for a browser check without the cabinet session.

It renders ``/profiles/new?…`` with the same application on a fake DB (as the tests
tests/api) and puts the HTML + ``src/api/static`` into the stand directory. The live counter on the
stand does not answer (there is no API) — the stand is for checking the JS of the form (chips, the wizard) and
the layout, not the figures. The customer hint (0046) answers with the static file
``profiles/customer-suggest`` (http.server ignores the query) — for the check of
customer_picker.js (``customers_e2e.js``), not of the search.

    python3 scripts/shots/stand_profile_form.py /tmp/twstand
    (cd /tmp/twstand && python3 -m http.server 8765 --bind 127.0.0.1)
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient  # noqa: E402

from src.api import deps  # noqa: E402
from src.api.cabinet import routes_profiles  # noqa: E402
from src.api.main import app  # noqa: E402
from tests.api.conftest import FakeSession  # noqa: E402

PAGES = {
    "form_manual.html": "/profiles/new?manual=1",
    "form_package.html": "/profiles/new?from_package=gradjevina",
    "start.html": "/profiles/new",
    "form_edit.html": "/profiles/p-1/edit",
}

# An edit of the template with the given filters of 0046. The customer names — from production.
EPS = "Акционарско друштво Електропривреда Србије, Београд"
_EDIT_PROFILE = SimpleNamespace(
    id="p-1",
    name="Video nadzor",
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
    contract_types=["services", "works"],
    customers_include=[EPS],
    customers_exclude=["Министарство одбране Републике Србије"],
)
_SUGGEST = {
    "items": [
        {"name": EPS, "tenders": 545},
        {"name": 'ЈАВНО КОМУНАЛНО ПРЕДУЗЕЋЕ "БЕОГРАДСКЕ ЕЛЕКТРАНЕ"', "tenders": 108},
        {"name": "ЕМС АД Београд", "tenders": 84},
    ]
}


async def _get_profile(*_a, **_kw):
    return _EDIT_PROFILE


async def _no_downgrade(*_a, **_kw):
    return []


def main() -> int:
    out = Path(sys.argv[1] if len(sys.argv) > 1 else "/tmp/twstand")
    out.mkdir(parents=True, exist_ok=True)
    app.dependency_overrides[deps.get_session_factory] = lambda: (
        lambda: FakeSession(user_locale="sr")
    )
    app.dependency_overrides[deps.get_optional_session] = lambda: {
        "uid": "u-1",
        "tid": "t-1",
    }
    routes_profiles.svc.get_profile = _get_profile
    routes_profiles.svc.downgrade_notice = _no_downgrade
    client = TestClient(app, follow_redirects=False)
    for name, url in PAGES.items():
        resp = client.get(url)
        resp.raise_for_status()
        (out / name).write_text(resp.text, encoding="utf-8")
        print(f"{name}: {url} → {resp.status_code}")
    shutil.copytree(ROOT / "src/api/static", out / "static", dirs_exist_ok=True)
    (out / "profiles").mkdir(exist_ok=True)
    (out / "profiles" / "customer-suggest").write_text(
        json.dumps(_SUGGEST, ensure_ascii=False), encoding="utf-8"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
