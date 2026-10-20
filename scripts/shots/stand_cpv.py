#!/usr/bin/env python3
"""A stand (uvicorn, a fake DB) for the measurement of /cpv-kodovi and of the group page /tenderi/cpv/NNN.

    python3 scripts/shots/stand_cpv.py 8771

The counters are synthetic (the layout, not production numbers): 153 groups ≥ the threshold, codes with
tenders — every 5th. The listing of the group — three placeholder rows.
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

from src.api import deps, repo  # noqa: E402
from src.api.cabinet.library_sectors import cpv_rows  # noqa: E402
from src.api.main import app  # noqa: E402
from src.api.public import cpv_directory, geo_cache  # noqa: E402
from tests.api.conftest import FakeSession  # noqa: E402


def _rows(*a, **k):
    now = datetime.now(timezone.utc)
    return [
        SimpleNamespace(
            id=f"t-{i}",
            title="Набавка радова на реконструкцији пута",
            customer_name="Општина Нови Сад",
            deadline_at=now + timedelta(days=5 + i),
            amount_eur=10000.0 * (i + 1),
            amount_original=None,
            currency_original=None,
        )
        for i in range(3)
    ]


async def _list(*a, **k):
    return _rows()


def main() -> None:
    app.dependency_overrides[deps.get_session_factory] = lambda: FakeSession
    app.dependency_overrides[deps.get_optional_session] = lambda: None
    tree = cpv_directory.tree("en")
    groups = {
        g["code"]: {"total": 10 + (i * 7) % 90, "open": 3, "open_sum_eur": 123456.0}
        for i, g in enumerate(g for d in tree for g in d["groups"])
        if i < 153
    }
    codes = {r["c"]: 1 + i % 17 for i, r in enumerate(cpv_rows()) if i % 5 == 0}
    future = time.monotonic() + 36000
    geo_cache._cpv_dir_cache = (future, {"groups": groups, "codes": codes})
    geo_cache._cpv_cache = (
        future,
        {
            d["code"]: {
                "total": 200,
                "open": 3,
                "open_sum_eur": None,
                "label": d["label"],
            }
            for d in tree
        },
    )
    geo_cache._region_cache = (future, {})
    geo_cache._sources_cache = (future, {"jnportal": "Portal javnih nabavki (UJN)"})
    repo.list_open_tenders_by_cpv = _list
    repo.list_closed_tenders_by_cpv = _list
    uvicorn.run(app, host="127.0.0.1", port=int(sys.argv[1]), log_level="warning")


if __name__ == "__main__":
    main()
