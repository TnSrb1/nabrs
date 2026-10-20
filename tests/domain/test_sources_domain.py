"""The domain of the platform registry (SRC-0a): the profile subscription and the selection of keys.

``apply_source_selection`` — the single writer of ``profiles.source_opt_in`` (R5: a tender is delivered only if it arrived AFTER the moment of the subscription). The expectations — literals.
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from src.domain.sources import (
    apply_source_selection,
    default_source_keys_for_new_profile,
    validate_source_keys,
)

T0 = datetime(2026, 9, 1, 10, 0, tzinfo=timezone.utc)
T1 = datetime(2026, 9, 30, 12, 0, tzinfo=timezone.utc)


def test_new_profile_gets_now_for_every_key():
    p = SimpleNamespace(enabled_source_keys=None, source_opt_in=None)
    apply_source_selection(p, ["jnportal", "ted_balkan"], T1)
    assert p.enabled_source_keys == ["jnportal", "ted_balkan"]
    assert p.source_opt_in == {
        "jnportal": "2026-09-30T12:00:00+00:00",
        "ted_balkan": "2026-09-30T12:00:00+00:00",
    }


def test_kept_key_keeps_old_moment_added_gets_now_removed_dropped():
    p = SimpleNamespace(
        enabled_source_keys=["jnportal", "old"],
        source_opt_in={
            "jnportal": "2026-09-01T10:00:00+00:00",
            "old": "2026-09-01T10:00:00+00:00",
        },
    )
    apply_source_selection(p, ["jnportal", "ted_balkan"], T1)
    assert p.enabled_source_keys == ["jnportal", "ted_balkan"]
    assert p.source_opt_in == {
        "jnportal": "2026-09-01T10:00:00+00:00",
        "ted_balkan": "2026-09-30T12:00:00+00:00",
    }


def test_kept_key_without_moment_stays_without_it():
    """A profile before 0045 without a record — the created_at fallback in the SQL, not “now”."""
    p = SimpleNamespace(enabled_source_keys=["jnportal"], source_opt_in={})
    apply_source_selection(p, ["jnportal"], T1)
    assert p.source_opt_in == {}


def test_readding_removed_key_is_a_new_subscription():
    p = SimpleNamespace(
        enabled_source_keys=["jnportal"],
        source_opt_in={"jnportal": "2026-09-01T10:00:00+00:00"},
    )
    apply_source_selection(p, [], T0)
    apply_source_selection(p, ["jnportal"], T1)
    assert p.source_opt_in == {"jnportal": "2026-09-30T12:00:00+00:00"}


def test_duplicates_and_empty_keys_collapse():
    p = SimpleNamespace(enabled_source_keys=[], source_opt_in={})
    apply_source_selection(p, ["jnportal", "", "jnportal"], T1)
    assert p.enabled_source_keys == ["jnportal"]


class _Session:
    def __init__(self, rows):
        self.rows = rows
        self.sql: list[str] = []

    async def execute(self, statement, params=None):
        self.sql.append(str(statement))
        rows = self.rows

        class _R:
            def all(self_inner):
                return list(rows)

        return _R()


def _row(key, codes, name=""):
    return SimpleNamespace(
        source_key=key, display_name=name, country_codes=codes, available_for_tiers=None
    )


@pytest.mark.asyncio
async def test_default_for_new_profile_is_only_rs_sources():
    """owner 30.09 item 2: by default only Serbian; multi-country — no."""
    session = _Session(
        [
            _row("jnportal", ["RS"]),
            _row("ted_balkan", ["BA", "ME", "RS"]),
            _row("ejn_ba", ["BA"]),
        ]
    )
    assert await default_source_keys_for_new_profile(session) == ["jnportal"]
    assert "WHERE enabled = true" in session.sql[0]


@pytest.mark.asyncio
async def test_validate_drops_unknown_keeps_enabled_and_existing():
    session = _Session([_row("jnportal", ["RS"])])
    assert await validate_source_keys(
        session, ["jnportal", "hacked", "paused", "jnportal"], keep=["paused"]
    ) == ["jnportal", "paused"]
