"""source_reconcile — daily reconciliation of the number of notices with the source (M2 of monitoring).

A row = (source, publication day): how many notices the source itself counted
(``source_count``, the API of the portal/TED) and how many we have (``our_count``,
``tenders_normalized``), when they were reconciled (``checked_at``). History is not lost (the ingest logs
are wiped by each rollout) and feeds the “reconciliation” column on ``/admin/sources``.

The table is GLOBAL (a metric of the source, RLS_MATRIX category category U+0432, like ``procurement_plan_items``):
neither ``tenant_id``, nor a policy, nor ``ENABLE``/``FORCE``. ONLY ingest may write
(superuser ``tendersrb``); ``tendersrb_app`` (bot/API, incl. the admin) — only ``SELECT``:
0021 granted ``DEFAULT PRIVILEGES`` to future tables, here INSERT/UPDATE/DELETE are revoked.

The migration runs after 0054 (asset_sales).

Revision ID: 0055
Revises: 0054
Create Date: 2026-10-16
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op

revision: str = "0055"
down_revision: Union[str, None] = "0054"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE source_reconcile (
            source_key text NOT NULL,
            day date NOT NULL,
            source_count integer NOT NULL,
            our_count integer NOT NULL,
            checked_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT source_reconcile_pk PRIMARY KEY (source_key, day),
            CONSTRAINT source_reconcile_counts_chk
                CHECK (source_count >= 0 AND our_count >= 0)
        )
        """)
    # 0021 granted DEFAULT PRIVILEGES (SELECT/INSERT/UPDATE/DELETE) — we leave only reading.
    op.execute(
        "REVOKE INSERT, UPDATE, DELETE, TRUNCATE ON source_reconcile FROM tendersrb_app"
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS source_reconcile")
