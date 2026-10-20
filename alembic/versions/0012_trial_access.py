"""trial_access — trial access with manual owner approval (STAGE T)

Two additive changes on top of 0011 (single head), without DROP/ALTER of existing
objects — rolled out to the LIVE prod (autodeploy), without locks and backfill:

1. ``tenants.access_until`` TIMESTAMPTZ NULL — the validity period of the tenant's access.
   **NULL = unlimited access** (existing demo/test tenants are NOT broken: after the rollout
   they keep NULL → matching skips them as before).
   Expired (``access_until <= now()``) → delivery stops
   (``STAGE_A_SQL`` in :mod:`src.workers.matching`).

2. Table ``trial_requests`` — requests for trial access from the cabinet. The decision is made by the owner in the bot (``@nabrs_bot``) via inline buttons: approve N days / reject.
   Statuses: ``pending`` (default) / ``approved`` / ``rejected``. ``days`` —
   the approved period; ``decided_at`` — the moment of the decision; ``admin_message_id`` —
   the id of the admin message in Telegram (for possible editing).

RLS: ``trial_requests`` has a direct ``tenant_id`` column → a direct policy
``tenant_isolation`` (as ``tender_user_status`` 0008 / ``profiles`` 0001):
``ENABLE ROW LEVEL SECURITY`` + ``USING (tenant_id =
current_setting('app.current_tenant_id')::uuid)``. The cabinet reads its own status
under the set ``app.current_tenant_id`` (RLS contract); writing decisions is a
privileged server operation (like delivery/reminders, owner role).

Revision ID: 0012
Revises: 0011
Create Date: 2026-06-11
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0012"
down_revision: Union[str, None] = "0011"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TS = postgresql.TIMESTAMP(timezone=True)
UUID = postgresql.UUID(as_uuid=True)
NOW = sa.text("now()")
GEN_UUID = sa.text("gen_random_uuid()")
DEFAULT_STATUS = sa.text("'pending'")


def upgrade() -> None:
    # 1) Validity period of the tenant’s access. NULL = unlimited (demo/test is not broken).
    op.add_column(
        "tenants",
        sa.Column("access_until", TS, nullable=True),
    )

    # 2) Trial access requests.
    op.create_table(
        "trial_requests",
        sa.Column("id", UUID, primary_key=True, server_default=GEN_UUID),
        sa.Column(
            "tenant_id",
            UUID,
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "status",
            sa.Text(),
            nullable=False,
            server_default=DEFAULT_STATUS,
        ),
        sa.Column("comment", sa.Text(), nullable=True),
        sa.Column("days", sa.Integer(), nullable=True),
        sa.Column("requested_at", TS, nullable=False, server_default=NOW),
        sa.Column("decided_at", TS, nullable=True),
        sa.Column("admin_message_id", sa.BigInteger(), nullable=True),
        sa.CheckConstraint(
            "status IN ('pending','approved','rejected')",
            name="trial_requests_status_check",
        ),
    )
    op.execute(
        "CREATE INDEX trial_requests_tenant_status_idx "
        "ON trial_requests (tenant_id, status)"
    )

    # RLS: direct tenant_id column → direct tenant_isolation policy (§4),
    # as on profiles (0001) and tender_user_status (0008).
    op.execute("ALTER TABLE trial_requests ENABLE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY tenant_isolation ON trial_requests "
        "USING (tenant_id = current_setting('app.current_tenant_id')::uuid)"
    )


def downgrade() -> None:
    # DROP TABLE removes the index and the tenant_isolation policy together with the table.
    op.execute("DROP TABLE IF EXISTS trial_requests CASCADE")
    op.drop_column("tenants", "access_until")
