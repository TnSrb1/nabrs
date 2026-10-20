#!/usr/bin/env python3
"""The stand of the section /tenderi/cpv/09 with a FLOW of rows: a live uvicorn, a fake DB, but the rows
of different length — the deadline badge (expired / today / N days / date) and the amount (present / absent /
in a currency / large). It is needed for the measurement of the columns of the right block (tender_row_columns_probe.js):
on production rows of different kinds would not fall into one selection.

    python3 scripts/shots/stand_geo_rows.py 8771
    # then: node tender_row_columns_probe.js http://127.0.0.1:8771/tenderi/cpv/09 "" 1280

It patches only the row selection and the statistics cache; the templates, CSS and markup are the real ones.
"""

from __future__ import annotations

import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import uvicorn  # noqa: E402

from src.api import deps, repo  # noqa: E402
from src.api.main import app  # noqa: E402
from src.api.public import geo_cache  # noqa: E402
from tests.api.conftest import FakeSession  # noqa: E402

NOW = datetime.now(timezone.utc)

# Deadline: expired / today / 1 / 2 / 5 / 12 / 40 days and without a deadline.
DEADLINES = [-3, 0, 1, 2, 5, 12, 40, None]
# Amount: none / small / large in EUR / in a currency / large in a currency.
AMOUNTS = [
    (None, None, None),
    (7328.0, None, None),
    (732789.0, None, None),
    (None, 1250000.0, "RSD"),
    (98765432.0, None, None),
]


def _row(i: int) -> SimpleNamespace:
    dl = DEADLINES[i % len(DEADLINES)]
    eur, orig, cur = AMOUNTS[i % len(AMOUNTS)]
    return SimpleNamespace(
        id=f"t-{i}",
        title=f"Набавка опреме за школу број {i}",
        customer_name="Општина Нови Сад",
        deadline_at=None if dl is None else NOW + timedelta(days=dl, hours=12),
        amount_eur=eur,
        amount_original=orig,
        currency_original=cur,
    )


def _async(v):
    async def f(*a, **k):
        return v

    return f


async def _open_rows(db, division, offset, limit):
    return [_row(i) for i in range(offset, offset + limit)]


async def _closed_rows(db, division, limit):
    return [_row(100 + i) for i in range(limit)]


def main() -> None:
    app.dependency_overrides[deps.get_session_factory] = lambda: FakeSession
    geo_cache._cpv_cache = (
        time.monotonic() + 36000,
        {"09": {"total": 70, "open": 20, "open_sum_eur": None, "label": "Услуге"}},
    )
    geo_cache._region_cache = (time.monotonic() + 36000, {})
    geo_cache._sources_cache = (time.monotonic() + 36000, {"jnportal": "Portal"})
    repo.list_open_tenders_by_cpv = _open_rows
    repo.list_closed_tenders_by_cpv = _closed_rows
    geo_cache.get_cpv_stats = _async(
        {"09": {"total": 70, "open": 20, "open_sum_eur": None, "label": "Услуге"}}
    )
    geo_cache.get_region_stats = _async({})
    geo_cache.get_source_labels = _async({"jnportal": "Portal"})
    print("stand: http://127.0.0.1:%s/tenderi/cpv/09" % sys.argv[1], flush=True)
    uvicorn.run(app, host="127.0.0.1", port=int(sys.argv[1]), log_level="warning")


if __name__ == "__main__":
    main()
