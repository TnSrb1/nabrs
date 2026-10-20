"""Unit: the ORM model TenderAward (migration 0018) — fields, types, FK, defaults.

Without a network/DB — only the declaration of the model on Base.metadata is checked: the name of the table, the set of columns, nullable/NOT NULL, the FK with ondelete CASCADE on tender_id, server_default on is_framework/has_changed/created_at.
"""

from sqlalchemy import BigInteger, Date, Integer, Numeric, SmallInteger

from src.db.base import Base
from src.db.models import TenderAward as RegisteredTA
from src.db.models.tender_award import TenderAward

_TABLE = TenderAward.__table__


def test_tablename() -> None:
    assert TenderAward.__tablename__ == "tender_awards"


def test_registered_on_metadata() -> None:
    assert "tender_awards" in Base.metadata.tables


def test_registered_in_package() -> None:
    assert RegisteredTA is TenderAward


def test_registered_in_all() -> None:
    import src.db.models as models

    assert "TenderAward" in models.__all__


def test_columns_present() -> None:
    cols = set(_TABLE.columns.keys())
    expected = {
        "id",
        "tender_id",
        "source_uid",
        "document_type_id",
        "lot_number",
        "lot_title",
        "winner_name",
        "winner_pib",
        "customer_pib",
        "customer_name",
        "decision_date",
        "award_amount_rsd",
        "award_amount_vat_rsd",
        "award_amount_eur",
        "currency_id",
        "contract_date",
        "is_framework",
        "duration_months",
        "has_changed",
        "raw",
        "captured_at",
        "created_at",
    }
    assert cols == expected


def test_id_is_primary_key() -> None:
    assert _TABLE.c.id.primary_key is True


def test_id_is_biginteger() -> None:
    assert isinstance(_TABLE.c.id.type, BigInteger)


def test_not_null_columns() -> None:
    assert _TABLE.c.tender_id.nullable is False
    assert _TABLE.c.source_uid.nullable is False
    assert _TABLE.c.document_type_id.nullable is False
    assert _TABLE.c.is_framework.nullable is False
    assert _TABLE.c.has_changed.nullable is False
    assert _TABLE.c.created_at.nullable is False


def test_nullable_columns() -> None:
    nullable = [
        "lot_number",
        "lot_title",
        "winner_name",
        "winner_pib",
        "customer_pib",
        "customer_name",
        "decision_date",
        "award_amount_rsd",
        "award_amount_vat_rsd",
        "award_amount_eur",
        "currency_id",
        "contract_date",
        "duration_months",
        "raw",
        "captured_at",
    ]
    for col in nullable:
        assert _TABLE.c[col].nullable is True, f"{col} should be nullable"


def test_tender_fk_cascade() -> None:
    fk = next(iter(_TABLE.c.tender_id.foreign_keys))
    assert fk.target_fullname == "tenders_normalized.id"
    assert fk.ondelete == "CASCADE"


def test_document_type_id_is_smallint() -> None:
    assert isinstance(_TABLE.c.document_type_id.type, SmallInteger)


def test_currency_id_is_smallint() -> None:
    assert isinstance(_TABLE.c.currency_id.type, SmallInteger)


def test_decision_date_is_date() -> None:
    assert isinstance(_TABLE.c.decision_date.type, Date)


def test_contract_date_is_date() -> None:
    assert isinstance(_TABLE.c.contract_date.type, Date)


def test_amount_columns_are_numeric_18_2() -> None:
    for col in ("award_amount_rsd", "award_amount_vat_rsd", "award_amount_eur"):
        t = _TABLE.c[col].type
        assert isinstance(t, Numeric), f"{col} should be Numeric"
        assert t.precision == 18
        assert t.scale == 2


def test_duration_months_is_integer() -> None:
    assert isinstance(_TABLE.c.duration_months.type, Integer)


def test_boolean_defaults() -> None:
    assert _TABLE.c.is_framework.server_default is not None
    assert _TABLE.c.has_changed.server_default is not None


def test_created_at_server_default() -> None:
    assert _TABLE.c.created_at.server_default is not None


def test_raw_is_jsonb() -> None:
    from sqlalchemy.dialects.postgresql import JSONB

    assert isinstance(_TABLE.c.raw.type, JSONB)
