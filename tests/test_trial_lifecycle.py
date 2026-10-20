"""The trial lifecycle: the access_until term revives the freeze axis (LAUNCH_AUDIT §4b).

signup writes access_until = trial_until (the test in tests/api/test_auth.py). Here we check the CONSEQUENCE on the domain axes that read this term:

- an active trial (access_until > now) → the access is there AND effective_tier→'pro';
- an expired trial (access_until <= now) → is_frozen=True (frozen), despite status='trial'. The freeze is checked BEFORE the tier (tender_ai_generate: the frozen gate before effective_tier), therefore the trial loses the pro rights.

Pure functions without a DB: they reproduce the order of the gate (frozen → tier), not a DB query.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from src.domain.access import access_active, is_frozen
from src.domain.tiers import effective_tier

NOW = datetime(2026, 7, 17, 12, 0, 0, tzinfo=timezone.utc)
TRIAL_DAYS = 14


def test_active_trial_has_access_and_pro_tier():
    """A trial within the term: the access is active, the effective tier — pro."""
    access_until = NOW + timedelta(days=TRIAL_DAYS)  # just registered
    assert access_active(access_until, NOW) is True
    assert is_frozen(access_until, NOW) is False
    assert effective_tier("trial", "lite") == "pro"


def test_expired_trial_is_frozen():
    """An expired trial is frozen — the mailing/AI stop, although status='trial'.

    The axis access_until (the freeze) and the axis status are independent: the status stays 'trial', but access_until is in the past → is_frozen=True. The AI gate checks frozen FIRST (tender_ai_generate: STATUS_FROZEN before effective_tier), therefore Pro is not given.
    """
    access_until = NOW - timedelta(days=1)  # the trial ended yesterday
    assert access_active(access_until, NOW) is False
    assert is_frozen(access_until, NOW) is True
    # effective_tier alone would still return pro — that is exactly why the frozen gate
    # must stand EARLIER; we check both facts, so that the order of the gate is explicit.
    assert effective_tier("trial", "lite") == "pro"


def test_trial_with_null_access_is_unbounded_active():
    """The regression of the defect: access_until=NULL = “active forever” (what all the trials had before the fix). Exactly this we no longer CREATE for new registrations — the test fixes that NULL really is never frozen."""
    assert is_frozen(None, NOW) is False
    assert is_frozen(None, NOW + timedelta(days=3650)) is False
