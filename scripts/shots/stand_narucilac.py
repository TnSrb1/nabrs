#!/usr/bin/env python3
"""The static stand of the “Naručioci” page (/cabinet/narucilac) for the measurement without a session.

It renders the page with the same application on a fake DB with the mocked service (as
tests/api/test_narucilac_page.py) and puts the HTML + ``src/api/static`` into the stand directory. The repository root is the CURRENT directory, therefore the “before” is taken from the clone of
``origin/main`` by the same script.

    python3 scripts/shots/stand_narucilac.py /tmp/nrstand [ru|sr] [sort] [q] [page]
    (cd /tmp/nrstand && python3 -m http.server 8766 --bind 127.0.0.1)
"""

from __future__ import annotations

import dataclasses
import shutil
import sys
from pathlib import Path

ROOT = Path.cwd()
sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient  # noqa: E402

from src.api import deps  # noqa: E402
from src.api.cabinet import routes_narucilac  # noqa: E402
from src.api.cabinet.narucilac_page_service import (  # noqa: E402
    DirectoryItemView,
    NarucilacPageView,
)
from src.api.main import app  # noqa: E402
from tests.api.conftest import FakeSession  # noqa: E402

_NAMES = [
    "Град Београд — Градска управа",
    "Општина Нови Сад",
    "ЈП „Електропривреда Србије“, Београд",
    "Клинички центар Србије",
    "Министарство одбране Републике Србије",
] * 5


def _page(lang: str, sort: str, q: str, page: int) -> NarucilacPageView:
    items = [
        DirectoryItemView(
            name=f"{n} {i}",
            pib=f"1002{i:05d}",
            awards=300 - i * 7,
            last_date_fmt=f"{(i % 27) + 1:02d}.09.2026",
        )
        for i, n in enumerate(_NAMES)
    ]
    fields = {f.name for f in dataclasses.fields(NarucilacPageView)}
    extra = dict(sort=sort, page=page, total_pages=3, total=45)
    kw = dict(status="success", term=q, candidates=items)
    kw.update({k: v for k, v in extra.items() if k in fields})
    return NarucilacPageView(**kw)


def main() -> int:
    out = Path(sys.argv[1] if len(sys.argv) > 1 else "/tmp/nrstand")
    lang = sys.argv[2] if len(sys.argv) > 2 else "ru"
    sort = sys.argv[3] if len(sys.argv) > 3 else "decisions_desc"
    q = sys.argv[4] if len(sys.argv) > 4 else ""
    page = int(sys.argv[5]) if len(sys.argv) > 5 else 1
    out.mkdir(parents=True, exist_ok=True)
    app.dependency_overrides[deps.get_session_factory] = lambda: (
        lambda: FakeSession(user_locale=lang)
    )
    app.dependency_overrides[deps.get_optional_session] = lambda: {
        "uid": "u-1",
        "tid": "t-1",
    }

    async def _fake(db, **_kw):
        return _page(lang, sort, q, page)

    routes_narucilac.svc.get_narucilac_page = _fake
    resp = TestClient(app, follow_redirects=False).get("/cabinet/narucilac")
    resp.raise_for_status()
    (out / "narucilac.html").write_text(resp.text, encoding="utf-8")
    shutil.copytree(ROOT / "src/api/static", out / "static", dirs_exist_ok=True)
    print(f"narucilac.html → {resp.status_code} ({lang}, sort={sort}, q={q!r})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
