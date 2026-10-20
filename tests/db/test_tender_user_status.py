"""Unit: the ORM model TenderUserStatus (migration 0008) — fields, FK, CHECK, default.

Without a network/DB — only the declaration of the model on ``Base.metadata`` is checked: the name of the table, the set of columns, NOT NULL and ondelete CASCADE on the FK (tenant_id/tender_id), the CHECK by exactly the 7 statuses of the funnel, server_default ``'new'`` and ``onupdate`` on ``updated_at``. The rollout of the migration and the RLS on a live DB are NOT checked here (there is no testcontainers infrastructure) — the RLS is enabled by migration 0008 (``ENABLE ROW LEVEL SECURITY`` + the direct policy ``tenant_isolation``), as for ``profiles`` (DB_SCHEMA §4).
"""

import re

from sqlalchemy import CheckConstraint

from src.db.base import Base
from src.db.models import TenderUserStatus as RegisteredTUS
from src.db.models.tender_status import STATUSES, TenderUserStatus

EXPECTED_STATUSES = (
    "new",
    "watching",
    "active",
    "submitted",
    "won",
    "lost",
    "skipped",
)


def _check_constraint() -> CheckConstraint:
    checks = [
        c
        for c in TenderUserStatus.__table__.constraints
        if isinstance(c, CheckConstraint)
    ]
    assert len(checks) == 1
    return checks[0]


def test_tablename() -> None:
    assert TenderUserStatus.__tablename__ == "tender_user_status"


def test_registered_on_metadata_and_in_package() -> None:
    assert "tender_user_status" in Base.metadata.tables
    # the import via the package (__init__.py) returns the same class
    assert RegisteredTUS is TenderUserStatus


def test_registered_in_all() -> None:
    import src.db.models as models

    assert "TenderUserStatus" in models.__all__


def test_columns_present() -> None:
    cols = set(TenderUserStatus.__table__.columns.keys())
    assert cols == {
        "id",
        "tenant_id",
        "tender_id",
        "status",
        "updated_at",
        "created_at",
    }


def test_id_is_primary_key() -> None:
    assert TenderUserStatus.__table__.c.id.primary_key is True


def test_not_null_columns() -> None:
    t = TenderUserStatus.__table__
    assert t.c.tenant_id.nullable is False
    assert t.c.tender_id.nullable is False
    assert t.c.status.nullable is False
    assert t.c.updated_at.nullable is False
    assert t.c.created_at.nullable is False


def test_tenant_fk_cascade() -> None:
    fk = next(iter(TenderUserStatus.__table__.c.tenant_id.foreign_keys))
    assert fk.target_fullname == "tenants.id"
    assert fk.ondelete == "CASCADE"


def test_tender_fk_cascade() -> None:
    fk = next(iter(TenderUserStatus.__table__.c.tender_id.foreign_keys))
    assert fk.target_fullname == "tenders_normalized.id"
    assert fk.ondelete == "CASCADE"


def test_status_server_default_new() -> None:
    col = TenderUserStatus.__table__.c.status
    assert col.server_default is not None
    assert "new" in str(col.server_default.arg)


def test_timestamps_server_default_now() -> None:
    t = TenderUserStatus.__table__
    assert t.c.updated_at.server_default is not None
    assert t.c.created_at.server_default is not None


def test_check_has_exactly_seven_statuses() -> None:
    sqltext = str(_check_constraint().sqltext)
    found = set(re.findall(r"'([a-z_]+)'", sqltext))
    assert found == set(EXPECTED_STATUSES)
    assert len(found) == 7


def test_check_values_match_funnel_order() -> None:
    sqltext = str(_check_constraint().sqltext)
    found = re.findall(r"'([a-z_]+)'", sqltext)
    assert tuple(found) == EXPECTED_STATUSES


def test_statuses_constant() -> None:
    assert isinstance(STATUSES, tuple)
    assert STATUSES == EXPECTED_STATUSES
    assert len(STATUSES) == 7


def test_statuses_constant_matches_check() -> None:
    sqltext = str(_check_constraint().sqltext)
    assert tuple(re.findall(r"'([a-z_]+)'", sqltext)) == STATUSES


def test_updated_at_has_onupdate() -> None:
    assert TenderUserStatus.__table__.c.updated_at.onupdate is not None


def test_created_at_has_no_onupdate() -> None:
    assert TenderUserStatus.__table__.c.created_at.onupdate is None
