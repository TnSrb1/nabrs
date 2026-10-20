"""The single time contract (`src/domain/tz.py`): the local Serbian ↔ UTC.

The contract we protect:
- `local_to_utc` (ingest): the wall-clock Serbian time → the true aware UTC, with
  the shift by DST (summer CEST −2 h, winter CET −1 h), and NOT a hard-code.
- `to_local` (display): aware/naive UTC → aware Europe/Belgrade.
- The round trip ingest→display returns the original wall-clock string of the portal
  (the regression of the double conversion).

The test WITHOUT a DB (pure functions).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from src.domain.tz import SERBIA_TZ, local_to_utc, to_local


# --------------------------------------------------------------------------- #
#  local_to_utc — the wall-clock Serbian → the true UTC (DST-aware)
# --------------------------------------------------------------------------- #
def test_local_to_utc_summer_is_minus_two_hours():
    """Summer (CEST +2): 11:00 Belgrade → 09:00 UTC."""
    # The wall-clock «06.08.2026 11:00:00» from the HTML card (naive).
    got = local_to_utc(datetime(2026, 8, 6, 11, 0, 0))
    assert got == datetime(2026, 8, 6, 9, 0, 0, tzinfo=timezone.utc)


def test_local_to_utc_winter_is_minus_one_hour():
    """Winter (CET +1): 11:00 Belgrade → 10:00 UTC (NOT the same 2 h as in summer)."""
    got = local_to_utc(datetime(2026, 1, 15, 11, 0, 0))
    assert got == datetime(2026, 1, 15, 10, 0, 0, tzinfo=timezone.utc)


def test_local_to_utc_result_is_aware():
    assert local_to_utc(datetime(2026, 8, 6, 11, 0, 0)).tzinfo is not None


def test_local_to_utc_dst_spring_gap_does_not_raise():
    """A non-existent time of the spring transition (the gap 02:00→03:00) does NOT raise.

    The transition 2026-03-29 02:00 CET → 03:00 CEST: 02:30 does not exist. fold=0 →
    a shift forward, without an exception. It only matters not to fall on the boundary data.
    """
    got = local_to_utc(datetime(2026, 3, 29, 2, 30, 0))
    assert got.tzinfo is not None


def test_local_to_utc_dst_autumn_overlap_does_not_raise():
    """An ambiguous time of the autumn transition (the duplicate 02:00→03:00) does NOT raise."""
    got = local_to_utc(datetime(2026, 10, 25, 2, 30, 0))
    assert got.tzinfo is not None


# --------------------------------------------------------------------------- #
#  to_local — UTC → local for display
# --------------------------------------------------------------------------- #
def test_to_local_summer_adds_two_hours():
    """09:00Z in summer → 11:00 Belgrade."""
    got = to_local(datetime(2026, 8, 6, 9, 0, 0, tzinfo=timezone.utc))
    assert got.strftime("%d.%m.%Y %H:%M") == "06.08.2026 11:00"


def test_to_local_winter_adds_one_hour():
    """10:00Z in winter → 11:00 Belgrade (CET +1)."""
    got = to_local(datetime(2026, 1, 15, 10, 0, 0, tzinfo=timezone.utc))
    assert got.strftime("%d.%m.%Y %H:%M") == "15.01.2026 11:00"


def test_to_local_naive_treated_as_utc():
    """A naive input is treated as UTC (only UTC is stored in the DB)."""
    got = to_local(datetime(2026, 8, 6, 9, 0, 0))
    assert got.strftime("%H:%M") == "11:00"


def test_to_local_none_is_none():
    assert to_local(None) is None


# --------------------------------------------------------------------------- #
#  The regression of the double conversion: ingest → display = the original wall-clock string
# --------------------------------------------------------------------------- #
def test_ingest_then_display_roundtrips_to_portal_string_summer():
    """«06.08.2026 11:00» of the portal → UTC → the display again «06.08.2026 11:00»."""
    stored = local_to_utc(datetime(2026, 8, 6, 11, 0, 0))  # as the ingest writes
    shown = to_local(stored).strftime("%d.%m.%Y %H:%M")  # as the display prints
    assert shown == "06.08.2026 11:00"


def test_ingest_then_display_roundtrips_winter():
    stored = local_to_utc(datetime(2026, 1, 15, 9, 30, 0))
    assert to_local(stored).strftime("%d.%m.%Y %H:%M") == "15.01.2026 09:30"


def test_default_tz_is_belgrade():
    assert str(SERBIA_TZ) == "Europe/Belgrade"


def test_to_local_preserves_instant():
    """The conversion changes the representation, but not the moment of time itself."""
    utc = datetime(2026, 8, 6, 9, 0, 0, tzinfo=timezone.utc)
    assert to_local(utc) - utc == timedelta(0)
