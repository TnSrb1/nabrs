"""Rendering of the REAL template /statistika?lang=sr with the sums of the production scale (the stand for stats_table_probe.js).

It does not go to the DB: the aggregates are substituted by the fixture from tests/api/test_statistika_page.py
(the sums — 9-digit, as on production, so that the line break of a row shows up).

Run from the root of the clone:
    python3 scripts/shots/stats_render_sr.py /tmp/cpv_stand/statistika.html
The stand’s static is served from a directory where ``static`` → ``src/api/static`` lies next to the HTML.
"""

from __future__ import annotations

import dataclasses
import sys
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from src.api.public.stats_page import AwardMonth, CustomerRow  # noqa: E402
from tests.api.test_statistika_page import STATS, _client  # noqa: E402


class _NoMonkeypatch:
    """The minimal replacement of pytest.monkeypatch: _client() only does setattr."""

    def setattr(self, obj, name, value):
        setattr(obj, name, value)


def main() -> None:
    stats = dataclasses.replace(
        STATS,
        award_months=(
            AwardMonth("06.2026", 1210, 987, Decimal("276507123.40"), 100),
            AwardMonth("07.2026", 412, 301, Decimal("22276507.00"), 40),
            AwardMonth("10.2026", 97, 12, Decimal("1234567.40"), 9),
        ),
        awards_sum_eur=Decimal("300018197.80"),
        awards_total=1719,
        awards_known=1300,
        top_customers=(CustomerRow("Grad Beograd", 3, Decimal("276507123")),),
    )
    r = _client(_NoMonkeypatch(), stats).get("/statistika?lang=sr")
    if r.status_code != 200:
        raise SystemExit(f"status {r.status_code}")
    out = Path(sys.argv[1])
    out.write_text(r.text, encoding="utf-8")
    print(f"OK: {out} ({len(r.text)} bytes)")


if __name__ == "__main__":
    main()
