"""channel_dead_notice_log — journal of “delivery channel died” incidents

Idempotency of the :mod:`src.workers.channel_dead_notify` worker: one email for
ONE incident. An incident = a continuous period while the tenant's channel is not alive
(predicate :func:`src.workers.matching.channel_states`).

“Open incident” model (not a snapshot, as in 0017):

* the channel died, no open incident → claim (INSERT) + email;
* the channel is still dead on the next cycle → the claim does not pass (an open
  incident already exists) → NO second email;
* the channel is restored → the incident is closed (``resolved_at = now()``);
* it dies again → no open incidents → a new claim + a new email.

Key — a PARTIAL UNIQUE ``(tenant_id, channel) WHERE resolved_at IS NULL``:
at most one incident per pair can be open at a time, while closed ones accumulate
without limit (history). It is exactly this that
``INSERT ... ON CONFLICT (tenant_id, channel) WHERE resolved_at IS NULL DO
NOTHING RETURNING id`` relies on — the partial index inference requires repeating its
predicate in ON CONFLICT.

A snapshot key (like access_until in 0017) would not fit here: “channel dead” has
no natural changing value — ``user_channels`` on unlinking may not change a single
column visible from outside (real case 7986f3b0: the status went to 'revoked' via manual
normalization). An open/closed incident survives this too.

Additive on top of 0030 (single head). No DROP/ALTER of existing objects —
only CREATE of a new table + partial UNIQUE + RLS policy (rolled out to the
LIVE prod).

RLS: a direct ``tenant_id`` column → a direct ``tenant_isolation`` policy (as
``reminder_log`` / ``subscription_reminder_log``). The worker writes privileged
(the owner bypasses RLS); the policy is kept for schema consistency. The cabinet does NOT
read the journal: the banner computes the predicate live, not from the journal.

Revision ID: 0031
Revises: 0030
Create Date: 2026-07-16
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0031"
down_revision: Union[str, None] = "0030"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TS = postgresql.TIMESTAMP(timezone=True)
UUID = postgresql.UUID(as_uuid=True)
NOW = sa.text("now()")
GEN_UUID = sa.text("gen_random_uuid()")

OPEN_INCIDENT_UNIQ = "channel_dead_notice_log_open_incident_uniq"


def upgrade() -> None:
    op.create_table(
        "channel_dead_notice_log",
        sa.Column("id", UUID, primary_key=True, server_default=GEN_UUID),
        sa.Column(
            "tenant_id",
            UUID,
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("channel", sa.Text(), nullable=False),
        sa.Column("notified_at", TS, nullable=False, server_default=NOW),
        sa.Column("resolved_at", TS, nullable=True),
    )

    # At most ONE open incident per (tenant, channel); closed ones — as many as
    # you like. The worker’s ON CONFLICT target (claim-then-send).
    op.create_index(
        OPEN_INCIDENT_UNIQ,
        "channel_dead_notice_log",
        ["tenant_id", "channel"],
        unique=True,
        postgresql_where=sa.text("resolved_at IS NULL"),
    )

    # RLS: direct tenant_id column → direct tenant_isolation policy (§4),
    # as on reminder_log / subscription_reminder_log.
    op.execute("ALTER TABLE channel_dead_notice_log ENABLE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY tenant_isolation ON channel_dead_notice_log "
        "USING (tenant_id = current_setting('app.current_tenant_id')::uuid)"
    )


def downgrade() -> None:
    # DROP TABLE removes the index and the tenant_isolation policy together with the table.
    op.execute("DROP TABLE IF EXISTS channel_dead_notice_log CASCADE")
