#!/usr/bin/env python3
"""The «Analitika» stand (/app/analitika) with a FLOW of rows: a live uvicorn, a fake DB.

It is needed for the measurement of a ready report: a click on a preset must return the page with the result
in the first screen (the anchor #an-result). Only the report selection is substituted; the preset links,
sorting, the form and the template are the real ones (the service and analitika.html are not patched).

    python3 scripts/shots/stand_analitika.py 8773
    # prints the cookie tb_session; then:
    bash scripts/shots/shot.sh --url http://127.0.0.1:8773/app/analitika --lang sr \
        --cookie tb_session=<cookie> --click 'a[data-an-preset]' --width 390 \
        --out /out/analitika_390.png --json /out/analitika_390.json
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import uvicorn  # noqa: E402

from src.analytics import report_builder as rb  # noqa: E402
from src.api import deps, security  # noqa: E402
from src.api.cabinet import analitika_service as svc  # noqa: E402
from src.api.cabinet import routes_analitika  # noqa: E402
from src.api.cabinet.analitika_service import AnalitikaPageView, RowView  # noqa: E402
from src.api.config import get_cabinet_settings  # noqa: E402
from src.api.main import app  # noqa: E402
from tests.api.conftest import FakeSession  # noqa: E402

SESS = {"uid": "u-1", "tid": "t-1"}
N_ROWS = 40


def _rows() -> list[RowView]:
    return [
        RowView(
            f"Општина број {i}",
            "",
            10 + i,
            5 + i % 3,
            f"{i * 1000:,} €".replace(",", " "),
            "",
            "",
            3 + i % 4,
            0,
        )
        for i in range(N_ROWS)
    ]


async def _fake_page(db, *, tid, params, lang):
    spec = rb.parse_spec(params)
    ran = "subject" in params
    total = RowView("", "", 500, 300, "1 000 000 €", "", "", 40, 0)
    return AnalitikaPageView(
        status="success",
        spec=spec,
        ran=ran,
        rows=_rows() if ran else [],
        total=total if ran else None,
        total_groups=N_ROWS,
        presets=svc._presets([], spec, ran),
        heads=svc.sort_heads(spec),
        export_href="/app/analitika.xlsx?" + "subject=" + spec.subject,
    )


def main() -> None:
    routes_analitika.svc.get_analitika_page = _fake_page
    app.dependency_overrides[deps.get_session_factory] = lambda: FakeSession
    app.dependency_overrides[deps.get_optional_session] = lambda: SESS
    cookie = security.sign_session(
        SESS["uid"], SESS["tid"], get_cabinet_settings().cabinet_secret_key, locale="sr"
    )
    print("tb_session=" + cookie, flush=True)
    uvicorn.run(app, host="127.0.0.1", port=int(sys.argv[1]), log_level="warning")


if __name__ == "__main__":
    main()
