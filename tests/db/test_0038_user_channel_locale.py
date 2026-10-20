"""Unit: migration 0038 (the language of the delivery channel — user_channels.locale).

Without a network/DB (``op.execute`` intercepted): we check the contract of the column by the SQL of the migration ITSELF — the only check that always works in the CI (the pattern — ``test_0037_admin_disable_subscriber``). The semantics of the rollout on a live PostgreSQL is proved by a run of `alembic upgrade/downgrade` on a one-off database (REPORT).

What we pin:
* the chain of revisions 0037 → 0038 and EXACTLY ONE head (two branches with the same number do not look like a git conflict, and alembic falls on production);
* the column is NULLABLE and WITHOUT a default: ``NULL`` = “inherit ``users.locale``”, not “language unknown” — with a ``DEFAULT``/``NOT NULL`` the inheritance would turn into a copy of the language at the moment of binding, and a language change in the cabinet would stop reaching Telegram;
* the CHECK passes NULL and exactly the canon of ``src.i18n_email.SUPPORTED`` (sr/ru/en) — the list in the migration is duplicated deliberately, a divergence is caught here;
* the migration does not touch the RLS: ``user_channels`` is already under the policy 0022 and FORCE 0032, one must not set them up again or weaken them;
* the downgrade removes both the CHECK and the column.
"""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest

from src.i18n_email import SUPPORTED

REPO_ROOT = Path(__file__).resolve().parents[2]
_MODULE_PATH = REPO_ROOT / "alembic" / "versions" / "0038_user_channel_locale.py"


def _load():
    spec = importlib.util.spec_from_file_location("migration_0038", _MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def migration():
    return _load()


def _sql(migration, monkeypatch, which="upgrade") -> str:
    calls: list[str] = []
    monkeypatch.setattr(migration.op, "execute", lambda sql: calls.append(sql))
    getattr(migration, which)()
    return "\n".join(calls)


def _flat(sql: str) -> str:
    return " ".join(sql.split())


def test_revision_chain(migration):
    assert migration.revision == "0038"
    assert migration.down_revision == "0037"


def _latest_revision_in_catalog() -> str:
    """The largest revision number in ``alembic/versions`` (by the file name).

    The expectation is derived from the DIRECTORY, not hard-coded: a literal of the “fresh head” expired on every new migration and demanded a shift by hand (0038 → 0039 → …), because of which the addition of a revision painted someone else’s test red.
    """
    revisions = sorted(
        path.name.split("_", 1)[0]
        for path in (REPO_ROOT / "alembic" / "versions").glob("[0-9]*_*.py")
    )
    assert revisions, "the alembic/versions directory is empty — the test checks nothing"
    return revisions[-1]


def test_single_alembic_head():
    """‼️ Two branches with one down_revision merge “cleanly”, and the rollout falls.

    The check goes by the directory of revisions (without a DB): ``alembic heads`` must return
    EXACTLY one line, and it must be the freshest revision of the directory — otherwise a new migration is hung not on the head but on the middle of the chain.
    """
    env = {
        **os.environ,
        "DATABASE_URL": "postgresql+asyncpg://offline:offline@localhost/tendersrb",
        "APP_DB_PASSWORD": "offline-render-only",
    }
    proc = subprocess.run(
        [sys.executable, "-m", "alembic", "heads"],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr
    heads = [line for line in proc.stdout.splitlines() if line.strip()]
    assert len(heads) == 1, f"one head was expected, received: {heads}"
    latest = _latest_revision_in_catalog()
    assert heads[0].startswith(latest), (
        f"the alembic head ({heads[0].split()[0]}) is not equal to the freshest revision of the "
        f"directory ({latest}) — the new migration branched off not from the head"
    )


def test_adds_nullable_column_without_default(migration, monkeypatch):
    """NULL = “inherit users.locale”. DEFAULT/NOT NULL would break the meaning."""
    upgrade = _flat(_sql(migration, monkeypatch))
    assert "ALTER TABLE user_channels ADD COLUMN IF NOT EXISTS locale text" in upgrade
    assert "SET NOT NULL" not in upgrade
    assert "SET DEFAULT" not in upgrade
    assert "DEFAULT" not in upgrade


def test_check_allows_null_and_exactly_supported_locales(migration, monkeypatch):
    upgrade = _flat(_sql(migration, monkeypatch))
    assert "ADD CONSTRAINT ck_user_channels_locale" in upgrade
    assert "CHECK (locale IS NULL OR locale IN (" in upgrade
    quoted = {f"'{code}'" for code in SUPPORTED}
    check = upgrade.split("locale IN (", 1)[1].split(")", 1)[0]
    assert {part.strip() for part in check.split(",")} == quoted


def test_migration_does_not_touch_rls_or_data(migration, monkeypatch):
    """The table is not new: the policy (0022) and FORCE (0032) are already in place."""
    upgrade = _flat(_sql(migration, monkeypatch))
    assert "CREATE POLICY" not in upgrade and "DROP POLICY" not in upgrade
    assert "ROW LEVEL SECURITY" not in upgrade
    assert "DELETE FROM" not in upgrade.upper()
    assert "UPDATE user_channels" not in upgrade


def test_downgrade_drops_constraint_then_column(migration, monkeypatch):
    downgrade = _flat(_sql(migration, monkeypatch, which="downgrade"))
    assert (
        "DROP CONSTRAINT IF EXISTS ck_user_channels_locale" in downgrade
    ), "The CHECK must be removed before the column"
    assert "DROP COLUMN IF EXISTS locale" in downgrade
    assert downgrade.index("DROP CONSTRAINT") < downgrade.index("DROP COLUMN")
