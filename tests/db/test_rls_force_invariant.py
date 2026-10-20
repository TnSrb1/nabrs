"""The CLAUDE.md invariant: every tenant-scoped table under FORCE ROW LEVEL SECURITY.

Why a separate test (a regression of 0031→0032). ``ENABLE ROW LEVEL SECURITY``
makes the policies bind everyone EXCEPT the table owner; ``FORCE``
extends them to the owner too. The application connects as ``tendersrb_app``
(NOBYPASSRLS), therefore under the app role there is NO visible difference between ENABLE and FORCE —
the integration RLS regression (``test_rls_isolation.py``) lets through a forgotten FORCE.
0031 went to production exactly so (``rls=t, force=f``), it was found by eye. This same test on the
first run found the SECOND such table — ``trial_requests``, skipped
by the list of 0021 already in 0021 (both closed by 0032).

The test looks not at the live DB, but at the SQL that the migrations EMIT: alembic in
the offline mode (``upgrade --sql``) unfolds the f-strings and the loops into the real
statements without a connection to PostgreSQL. Therefore the check goes in the ordinary CI on
every PR — unlike ``test_rls_isolation.py``, which needs
``TEST_RLS_DATABASE_URL`` (there is none in the CI, the tests are skipped).

The list of tables is hard-coded nowhere: it is derived ENTIRELY from the emitted SQL.
Exactly why the test caught trial_requests — a handwritten list (as in 0021) had lost it.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

# The render window is split in two: 0016_template_library emits data-INSERTs with
# JSONB literals, for which SQLAlchemy has no literal renderer, and the offline
# base:head fails. We render base:0015 and 0016:head — the union lacks only the
# statements of 0016 itself. The guarantee that the hole is empty — the test
# test_migration_0016_touches_no_rls below.
_WINDOWS = (("base", "0015"), ("0016", "head"))

_POLICY_RE = re.compile(r"CREATE POLICY tenant_isolation ON (\w+)", re.IGNORECASE)
_FORCE_RE = re.compile(r"ALTER TABLE (\w+) FORCE ROW LEVEL SECURITY", re.IGNORECASE)
_NO_FORCE_RE = re.compile(
    r"ALTER TABLE (\w+) NO FORCE ROW LEVEL SECURITY", re.IGNORECASE
)


def _render(start: str, end: str) -> str:
    """The SQL that alembic emits on upgrade start→end (offline, without a DB).

    DATABASE_URL is needed by env.py to choose the dialect (there will be no connection),
    APP_DB_PASSWORD — 0021 fails hard without it.
    """
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
        f"alembic upgrade {start}:{end} --sql (offline) did not render:\n"
        + proc.stderr
    )
    return proc.stdout


@pytest.fixture(scope="module")
def upgrade_sql() -> str:
    return "\n".join(_render(start, end) for start, end in _WINDOWS)


def _force_state(sql: str) -> dict[str, bool]:
    """The final relforcerowsecurity by table — the statements in the order of the rollout."""
    state: dict[str, bool] = {}
    for line in sql.splitlines():
        if match := _NO_FORCE_RE.search(line):
            state[match.group(1)] = False
        elif match := _FORCE_RE.search(line):
            state[match.group(1)] = True
    return state


def test_migration_0016_touches_no_rls():
    """0016 falls out of the render window — we check that it had nothing to say about the RLS.

    If 0016 ever starts touching the RLS, the ``_WINDOWS`` window stops being complete and the common invariant below leaks silently. The test is the guard of this hole.
    """
    source = (REPO_ROOT / "alembic/versions/0016_template_library.py").read_text()
    assert "ROW LEVEL SECURITY" not in source.upper(), (
        "0016 started touching the RLS, but it is outside the render window (_WINDOWS) — "
        "the FORCE invariant will not see it. Fix the window."
    )
    assert (
        "CREATE POLICY" not in source.upper()
    ), "0016 started creating policies, but it is outside the render window (_WINDOWS)."


def test_every_tenant_isolated_table_is_force_rls(upgrade_sql):
    """Every table with the policy tenant_isolation is rolled out with FORCE.

    This is the test that would have caught 0031 on the PR: it emits CREATE POLICY
    tenant_isolation ON channel_dead_notice_log without a paired FORCE.

    The tables without the policy tenant_isolation (the global ones — admin_audit_log,
    tenders_normalized, billing_webhook_events) are not in the check: they are not
    tenant-scoped, the RLS on them is absent deliberately.
    """
    policied = set(_POLICY_RE.findall(upgrade_sql))
    assert len(policied) > 20, (
        f"suspiciously few tables with tenant_isolation ({len(policied)}) — "
        "it looks like the render of the window broke and the test checks nothing"
    )

    force_state = _force_state(upgrade_sql)
    unforced = sorted(t for t in policied if not force_state.get(t, False))
    assert not unforced, (
        "tenant-scoped tables with the policy tenant_isolation, but without FORCE ROW "
        f"LEVEL SECURITY: {unforced}. Under the app role (NOBYPASSRLS) the hole is not visible, "
        "but the table owner reads other tenants' rows. Add "
        "'ALTER TABLE <t> FORCE ROW LEVEL SECURITY' to the migration."
    )


@pytest.mark.parametrize("table", ["channel_dead_notice_log", "trial_requests"])
def test_force_rls_tables_of_0032(upgrade_sql, table):
    """The pinpoint regression of 0032 over both closed tables.

    It duplicates the common invariant above deliberately: the common test protects the future
    tables, these two nail the concrete production defects
    (channel_dead_notice_log — 0031, trial_requests — 0021).
    """
    assert (
        _force_state(upgrade_sql).get(table) is True
    ), f"{table} must be rolled out with FORCE ROW LEVEL SECURITY (migration 0032)"
