"""tender_user_status — per-user tender status funnel (v6 port)

The tender status within the funnel of a SPECIFIC tenant: the subscriber's mark on a tender.
One status per pair (tenant_id, tender_id). Foundation for the buttons/summary/
reminders (handlers and workers — separate PRs; here only the schema).

A 7-value funnel (v6 port of ``STATUS_LABELS``):
    new       — 🆕 New (default, before any user action)
    watching  — 👀 Studying
    active    — ✅ Participating
    submitted — 📨 Bid submitted
    won       — 🏆 Won
    lost      — 😔 Lost
    skipped   — ❌ Skipped

This is NOT ``tenders_normalized.status`` (the tender's own status from the source:
active/awarded/cancelled/closed) — here it is the per-tenant user funnel.

Additive on top of 0007 (single head). No DROP/ALTER of existing objects —
only CREATE of a new table + indexes + an RLS policy (rolled out to the LIVE prod
via autodeploy).

RLS: a direct ``tenant_id`` column → a direct ``tenant_isolation`` policy (as §4
on ``profiles``): ``ENABLE ROW LEVEL SECURITY`` + ``USING (tenant_id =
current_setting('app.current_tenant_id')::uuid)``.

Revision ID: 0008
Revises: 0007
Create Date: 2026-06-05
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0008"
down_revision: Union[str, None] = "0007"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TS = postgresql.TIMESTAMP(timezone=True)
UUID = postgresql.UUID(as_uuid=True)
NOW = sa.text("now()")
GEN_UUID = sa.text("gen_random_uuid()")
DEFAULT_STATUS = sa.text("'new'")


def upgrade() -> None:
    op.create_table(
        "tender_user_status",
        sa.Column("id", UUID, primary_key=True, server_default=GEN_UUID),
        sa.Column(
            "tenant_id",
            UUID,
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "tender_id",
            UUID,
            sa.ForeignKey("tenders_normalized.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "status",
            sa.Text(),
            nullable=False,
            server_default=DEFAULT_STATUS,
        ),
        sa.Column("updated_at", TS, nullable=False, server_default=NOW),
        sa.Column("created_at", TS, nullable=False, server_default=NOW),
        sa.CheckConstraint(
            "status IN ('new','watching','active','submitted','won','lost','skipped')",
            name="tender_user_status_status_check",
        ),
    )
    op.execute(
        "CREATE UNIQUE INDEX tender_user_status_tenant_tender_uniq "
        "ON tender_user_status (tenant_id, tender_id)"
    )
    op.execute(
        "CREATE INDEX tender_user_status_tenant_status_idx "
        "ON tender_user_status (tenant_id, status)"
    )

    # RLS: direct tenant_id column → direct tenant_isolation policy (§4),
    # as on profiles (0001).
    op.execute("ALTER TABLE tender_user_status ENABLE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY tenant_isolation ON tender_user_status "
        "USING (tenant_id = current_setting('app.current_tenant_id')::uuid)"
    )


def downgrade() -> None:
    # DROP TABLE removes the indexes and the tenant_isolation policy together with the table.
    op.execute("DROP TABLE IF EXISTS tender_user_status CASCADE")
