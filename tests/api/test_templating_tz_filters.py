"""The Jinja filters of the time conversion of the cabinet (`dt_local`/`d_local`/`localtime`).

The contract is the same as for `format_deadline`/the CSV export: the moment from the DB (UTC) is printed in Belgrade time. One conversion contour — `src/domain/tz.to_local`.
We render through the template environment, to check the registered filter itself.
"""

from __future__ import annotations

from datetime import datetime, timezone

from src.api.templating import templates

_ENV = templates.env


def _render(src: str, **ctx) -> str:
    return _ENV.from_string(src).render(**ctx)


def test_dt_local_summer_converts_to_belgrade():
    dt = datetime(2026, 8, 6, 9, 0, tzinfo=timezone.utc)  # 09:00Z summer
    assert _render("{{ dl | dt_local }}", dl=dt) == "06.08.2026 11:00"


def test_dt_local_winter_offset_differs():
    dt = datetime(2026, 1, 15, 10, 0, tzinfo=timezone.utc)  # 10:00Z winter
    assert _render("{{ dl | dt_local }}", dl=dt) == "15.01.2026 11:00"


def test_d_local_date_only():
    dt = datetime(2026, 8, 6, 9, 0, tzinfo=timezone.utc)
    assert _render("{{ dl | d_local }}", dl=dt) == "06.08.2026"


def test_filters_none_render_empty_not_crash():
    assert _render("[{{ dl | dt_local }}]", dl=None) == "[]"
    assert _render("[{{ dl | d_local }}]", dl=None) == "[]"


def test_localtime_returns_datetime_for_custom_strftime():
    """`localtime` gives an aware datetime — for a non-standard format (the agenda)."""
    dt = datetime(2026, 8, 6, 9, 0, tzinfo=timezone.utc)
    assert (
        _render("{{ (e | localtime).strftime('%d.%m %H:%M') }}", e=dt) == "06.08 11:00"
    )


def test_naive_input_treated_as_utc():
    dt = datetime(2026, 8, 6, 9, 0)  # naive → UTC
    assert _render("{{ dl | dt_local }}", dl=dt) == "06.08.2026 11:00"
