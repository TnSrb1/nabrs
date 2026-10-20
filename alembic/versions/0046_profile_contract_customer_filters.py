"""Profile filters “Contract type” and “Customer: follow / exclude”.

Three NULL arrays in ``profiles`` (NULL/empty = no restriction, like
``procedure_types``):

* ``contract_types``    — ``goods|services|works``: a tender passes if its
  ``tenders_normalized.contract_type`` is in the list (NULL on the tender — the restriction is not applicable, NULL semantics of stage A);
* ``customers_include`` — “follow” mode: a tender of a customer from the list passes even
  without a match by topic/keywords;
* ``customers_exclude`` — tenders of a customer from the list do not come.

The customer is stored by NAME, as the source writes it (for the chips in the form), and is compared
by the key ``sql_customer_key`` (``src/workers/matching.py``) on both sides. Not the tax ID: ``customer_tax_id`` is filled for 0 out of 10,300 tenders over
90 days (measurement 01.10.2026), a key by tax ID would match nothing.

RLS is not touched: the table is already tenant-scoped (policy + ENABLE + FORCE), and the
grants of ``tendersrb_app`` from 0021 are table-level and apply to the new columns.
No backfill: NULL = the previous behavior.

Revision ID: 0046
Revises: 0045
Create Date: 2026-10-01
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import ARRAY

revision: str = "0046"
down_revision: Union[str, None] = "0045"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_COLUMNS = ("contract_types", "customers_include", "customers_exclude")


def upgrade() -> None:
    for name in _COLUMNS:
        op.add_column("profiles", sa.Column(name, ARRAY(sa.Text()), nullable=True))


def downgrade() -> None:
    for name in reversed(_COLUMNS):
        op.drop_column("profiles", name)
