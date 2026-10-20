"""Channel locale of delivery: user_channels.locale (NULL = inherit users.locale)

Stage 2c batch A (#481) taught Telegram delivery to take the card language from
``users.locale`` — that is, from the CABINET language. Owner’s decision: «pass the language on connection
according to the interface, but the user must be able to change the language in TG by themselves».
Hence the language of the channel itself.

‼️ The column is NULLABLE and WITHOUT a default, because ``NULL`` here means
“inherit ``users.locale``”, NOT “language unknown”. Copying the language at the moment of
binding (``locale = users.locale``) looks simpler, but breaks consistency:
a subscriber who changed the language in the cabinet would keep receiving Telegram in the old
language, although they chose nothing in the bot. With inheritance by default everything stays consistent,
and an explicit choice in the bot (``/language``) is pinned to the channel and lives on its own —
this is exactly the requested flexibility.

The third language axis is NOT introduced: ``users.locale`` (interface) and
``profiles.target_language`` (AI content) stay as they are; the channel ``locale`` is a refinement of the FIRST
axis for a specific delivery recipient.

The CHECK repeats the canon of the three supported languages (``src.i18n_email.SUPPORTED``:
sr/ru/en) — the same set as the language switcher in the cabinet and in the bot. The NULL CHECK
passes (``locale IS NULL OR ...``) — this is the standard value, not a violation.

The table is NOT new: the ``tenant_isolation`` policy (join via users, 0022) and
``FORCE ROW LEVEL SECURITY`` (0032) on ``user_channels`` are already in place — we do not
create or weaken them again. The migration is additive (ADD COLUMN NULL without a default does not
rewrite the table), rolls out on the live production, does not touch data.

Revision ID: 0038
Revises: 0037
Create Date: 2026-08-02
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op

revision: str = "0038"
down_revision: Union[str, None] = "0037"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# The canon of interface languages — the same as src.i18n_email.SUPPORTED (sr/ru/en).
# The app code cannot be imported into a migration (a migration must be
# reproducible on any code revision), so the list is duplicated here
# deliberately; a divergence will be caught by the migration test.
_LOCALES_SQL_LIST = "'sr', 'ru', 'en'"


def upgrade() -> None:
    # ADD COLUMN IF NOT EXISTS — the migration is idempotent on a repeated rollout.
    op.execute("ALTER TABLE user_channels ADD COLUMN IF NOT EXISTS locale text")
    op.execute(
        f"""
        ALTER TABLE user_channels
          ADD CONSTRAINT ck_user_channels_locale
          CHECK (locale IS NULL OR locale IN ({_LOCALES_SQL_LIST}))
        """
    )


def downgrade() -> None:
    # Reverse order: first the CHECK, then the column.
    op.execute(
        "ALTER TABLE user_channels DROP CONSTRAINT IF EXISTS ck_user_channels_locale"
    )
    op.execute("ALTER TABLE user_channels DROP COLUMN IF EXISTS locale")
