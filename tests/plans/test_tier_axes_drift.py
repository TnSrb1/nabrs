"""Regression: THREE tier axes diverged — we fix the FACT, not the desired.

The project rules, the invariant “do NOT mix the tier axes”: the showcase
(``src/plans/catalog.py``) ≠ the gating (``src/ai/usage.py`` ``_TIER_LIMITS``) ≠
the model choice (``src/ai/client.py`` ``LEVEL_TIER``). The axes DIVERGED and live so
in production: the reconciliation was prescribed by VG-12 before VG-11, VG-11 went without the reconciliation.

‼️ This file does NOT claim that the axes are consistent, and does NOT fix the divergence.
It NAILS the current delta by a literal so that:
- the divergence does not grow silently (a new divergence → the test turns red);
- the owner has an exact list of what to decide.

Changing the fixed deltas is the owner's decision (an ADR), not the worker's. If the test
turned red because the axis was changed deliberately — first an ADR, then the literal here.

The CHECKs are read not from a live DB, but from the SQL that the migrations EMIT in the
offline mode (``alembic upgrade --sql``): the f-strings and the loops are unfolded into the
real statements without a connection to PostgreSQL, therefore the test runs in the ordinary
CI (the pattern — ``tests/db/test_rls_force_invariant.py``).
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from src.ai.client import LEVEL_TIER
from src.ai.usage import _TIER_LIMITS
from src.api.tiers import PROFILE_LIMITS
from src.domain.tiers import TIER_ORDER, normalize_tier
from src.plans.catalog import CATALOG, tier_to_plan_key

REPO_ROOT = Path(__file__).resolve().parents[2]

# The render window is split in two for the same reason as in test_rls_force_invariant:
# 0016_template_library emits data-INSERTs with JSONB literals, for which
# SQLAlchemy has no literal renderer, and the offline base:head falls. Both migrations
# interesting to us are inside the windows: 0001 (subscriptions_tier_check) in base:0015,
# 0027 (ck_tenants_tier) in 0016:head.
_WINDOWS = (("base", "0015"), ("0016", "head"))


def _render(start: str, end: str) -> str:
    """The SQL that alembic emits on upgrade start→end (offline, without a DB)."""
    env = {
        **os.environ,
        "DATABASE_URL": "postgresql+asyncpg://offline:offline@localhost/tendersrb",
        "APP_DB_PASSWORD": "offline-render-only",
    }
    proc = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", f"{start}:{end}", "--sql"],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, (
        f"alembic upgrade {start}:{end} --sql (offline) did not render:\n" + proc.stderr
    )
    return proc.stdout


@pytest.fixture(scope="module")
def upgrade_sql() -> str:
    return "\n".join(_render(start, end) for start, end in _WINDOWS)


def _check_tiers(sql: str, constraint: str) -> frozenset[str]:
    """The set of tier literals from ``CONSTRAINT <name> CHECK (tier IN (...))``.

    It tolerates both rendering styles: 0001 gives the constraint inline in CREATE TABLE without spaces after the commas, 0027 — as a separate ALTER with a line break.
    """
    match = re.search(
        rf"CONSTRAINT\s+{constraint}\s+CHECK\s*\(\s*tier\s+IN\s*\(([^)]*)\)",
        sql,
        re.IGNORECASE | re.DOTALL,
    )
    assert match is not None, (
        f"CHECK {constraint} not found in the emitted SQL — either the migration "
        "was rewritten, or the render window broke and the test checks nothing"
    )
    return frozenset(re.findall(r"'([^']+)'", match.group(1)))


# --- Axis 1: the showcase -------------------------------------------------------


def test_axis_showcase_sells_four_plans_and_four_tiers():
    """The showcase sells 4 plans and can issue exactly 4 internal tiers.

    ``starter`` is absent here DELIBERATELY (legacy, not sold) — see
    the divergence detector below.
    """
    assert {p.key for p in CATALOG} == {"free", "pro", "business", "enterprise"}
    assert {p.tier for p in CATALOG} == {"lite", "pro", "business", "enterprise"}


def test_axis_showcase_maps_legacy_starter_to_free():
    """starter cannot be bought, but a tenant with such a tier is highlighted in the cabinet as Free."""
    assert tier_to_plan_key("starter") == "free"
    assert tier_to_plan_key("free") == "free"  # 'free' is not a tier: unknown → free
    assert tier_to_plan_key(None) == "free"


# --- Axis 2: the gating -------------------------------------------------------


def test_axis_gating_knows_five_tiers():
    """``_TIER_LIMITS`` — 5 levels (ADR-018), including the unsellable starter."""
    assert set(_TIER_LIMITS) == {"lite", "starter", "pro", "business", "enterprise"}


def test_axis_gating_matches_domain_canon():
    """The gating and the canon ``TIER_ORDER`` coincide — these two axes have NOT diverged."""
    assert set(_TIER_LIMITS) == set(TIER_ORDER)
    assert set(PROFILE_LIMITS) == set(TIER_ORDER)


def test_axis_gating_normalizes_free_to_lite():
    """``free`` for the gating is not a tier: it collapses into lite (the fallback of the normalizer)."""
    assert normalize_tier("free") == "lite"


# --- Axis 3: the CHECKs in the DB (by the SQL of the migrations, without a connection) ---------------


def test_axis_db_check_tenants_is_domain_canon(upgrade_sql):
    """``ck_tenants_tier`` (0027) = ``TIER_ORDER``. The literal of the migration ≡ the runtime canon."""
    assert _check_tiers(upgrade_sql, "ck_tenants_tier") == frozenset(TIER_ORDER)


def test_axis_db_check_subscriptions_still_allows_free(upgrade_sql):
    """``subscriptions_tier_check`` (0001) is wider than the canon by ``free``.

    Nailed by a literal: this is a KNOWN delta, not a typo. See the detector below.
    """
    assert _check_tiers(upgrade_sql, "subscriptions_tier_check") == frozenset(
        {"free", "lite", "starter", "pro", "business", "enterprise"}
    )


# --- Axis 4: the model choice --------------------------------------------------


def test_axis_level_tier_is_a_different_namespace():
    """``LEVEL_TIER`` — the analysis level → the tier of the MODEL, not the plan.

    It must not intersect with the plan names by keys or values:
    a common name = the first step to mixing the axes (the invariant from the project rules).
    """
    assert set(LEVEL_TIER.values()) == {"cheap", "balanced", "premium"}
    plan_tiers = set(TIER_ORDER) | {"free"}
    assert not (set(LEVEL_TIER.values()) & plan_tiers)
    assert not (set(LEVEL_TIER) & plan_tiers)


# --- The divergence detector ----------------------------------------------------
#
# Below is the ONLY place where the delta between the axes is recorded explicitly. The list
# is fixed as of 2026-07-16 and handed to the owner (ADR). The test turns red both
# when the divergence GREW, and when it was FIXED bypassing the ADR.


def test_drift_starter_gated_and_checked_but_not_sold(upgrade_sql):
    """KNOWN DELTA №1: ``starter`` — in the gating and in both CHECKs, not on the showcase.

    The consequence: ``gate_starter`` (routes_profiles) is in fact pro+, because the starter tier cannot be bought — it is obtained only by legacy tenants.
    Fixing it (remove the tier or return it to the showcase) is the owner’s decision, not the worker’s.
    """
    assert "starter" in _TIER_LIMITS
    assert "starter" in _check_tiers(upgrade_sql, "ck_tenants_tier")
    assert "starter" in _check_tiers(upgrade_sql, "subscriptions_tier_check")
    assert "starter" not in {p.tier for p in CATALOG}
    assert "starter" not in {p.key for p in CATALOG}


def test_drift_free_allowed_by_subscriptions_check_but_banned_by_tenants_check(
    upgrade_sql,
):
    """KNOWN DELTA №2: ``free`` is allowed by one CHECK and forbidden by another.

    It does not fire: the billing maps the plan free → the tier lite (``catalog.Plan.tier``), and ``normalize_tier`` collapses any non-canon into lite. That is, 'free' is the public slug of the plan, not a tier, and it does not get into tenants.tier.
    The divergence of the CHECKs themselves is nevertheless REAL and is decided by the owner.
    """
    assert "free" in _check_tiers(upgrade_sql, "subscriptions_tier_check")
    assert "free" not in _check_tiers(upgrade_sql, "ck_tenants_tier")
    assert "free" not in _TIER_LIMITS
    assert next(p for p in CATALOG if p.key == "free").tier == "lite"


def test_drift_delta_between_axes_is_exactly_the_known_list(upgrade_sql):
    """The summary detector: the delta between the axes == exactly the fixed list.

    A NEW divergence will appear (a tier in the gating without a CHECK, a tier on the showcase without a gate, one more legacy alias in a CHECK) — the set will diverge from the literal and the test will turn red. This is a guard, not a verdict: the literal may be extended only together with an ADR (the owner’s decision).
    """
    showcase = {p.tier for p in CATALOG}
    gating = set(_TIER_LIMITS)
    tenants_check = _check_tiers(upgrade_sql, "ck_tenants_tier")
    subs_check = _check_tiers(upgrade_sql, "subscriptions_tier_check")

    # The gating is wider than the showcase by exactly the legacy starter — and by nothing else.
    assert gating - showcase == {"starter"}
    # The showcase sells nothing outside the gating.
    assert not showcase - gating
    # The tenants' CHECK ≡ the gating: this pair has NOT diverged, we guard so that it does not start to.
    assert not tenants_check ^ gating
    # The CHECK of the legacy subscriptions is wider than the gating by exactly the public slug 'free'.
    assert subs_check - gating == {"free"}
    assert not gating - subs_check
