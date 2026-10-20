"""Tender changes (RED): diff journal, notification dedup, document recheck marker

AI analysis, step 3 (owner decision 19.10): a subscriber to whom a tender has already been delivered must learn that the tender has changed (deadline, amount, documentation files), instead of silently receiving a stale analysis. Three service tables, all GLOBAL in access — written and read only by the worker under the superuser ``tendersrb`` (ingest, tender_change_notifier):

* ``tender_changes`` — journal of structural diffs "was → became" (``diff`` jsonb, format — ``src/domain/tender_changes.py``); public tender data, no tenant.
* ``tender_change_sent`` — one row per (tender, recipient): up to which event and when the recipient was notified. Once a day per pair — the condition in the notifier's query. ‼️ Column ``notified_tenant`` — NOT ``tenant_id`` and deliberately NOT under RLS: the table is read/written only by the superuser worker, and the app role has ALL rights revoked (bot/API do not see it at all). An RLS policy would be decoration on top of REVOKE.
* ``tender_docs_check`` — when the tender's document list was last rechecked (no more than once a day per tender).

Only CREATE (additive); no DROP/ALTER of existing objects.

Revision ID: 0058
Revises: 0057
Create Date: 2026-10-19
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op

revision: str = "0058"
down_revision: Union[str, None] = "0057"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE tender_changes (
            id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            tender_id uuid NOT NULL REFERENCES tenders_normalized(id) ON DELETE CASCADE,
            detected_at timestamptz NOT NULL DEFAULT now(),
            kind text NOT NULL,
            diff jsonb NOT NULL
        )
        """)
    op.execute("CREATE INDEX tender_changes_tender_idx ON tender_changes (tender_id, id)")
    op.execute("CREATE INDEX tender_changes_detected_idx ON tender_changes (detected_at)")
    op.execute("""
        CREATE TABLE tender_change_sent (
            tender_id uuid NOT NULL REFERENCES tenders_normalized(id) ON DELETE CASCADE,
            notified_tenant uuid NOT NULL,
            last_change_id bigint NOT NULL,
            sent_at timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (tender_id, notified_tenant)
        )
        """)
    op.execute("""
        CREATE TABLE tender_docs_check (
            tender_id uuid PRIMARY KEY REFERENCES tenders_normalized(id) ON DELETE CASCADE,
            checked_at timestamptz NOT NULL DEFAULT now()
        )
        """)
    op.execute(
        "REVOKE INSERT, UPDATE, DELETE, TRUNCATE ON tender_changes, tender_docs_check "
        "FROM tendersrb_app"
    )
    op.execute("REVOKE ALL ON tender_change_sent FROM tendersrb_app")
    op.execute("REVOKE ALL ON SEQUENCE tender_changes_id_seq FROM tendersrb_app")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS tender_docs_check")
    op.execute("DROP TABLE IF EXISTS tender_change_sent")
    op.execute("DROP TABLE IF EXISTS tender_changes")
