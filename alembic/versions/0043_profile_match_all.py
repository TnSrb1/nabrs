"""An explicit profile mode “All tenders” (``profiles.match_all``).

Until this revision “show the whole flow” was not a mode but a SIDE EFFECT of emptiness:
a profile without keywords and without classifications passed all the “filter not set” branches in
``STAGE_A_SQL`` and ``stage_b_filter`` (``if not groups:
return True``) — and received everything. It was IMPOSSIBLE to tell
“the client deliberately chose the whole market” from “the client did not understand how to configure it and abandoned the form”.

The cost of this implicitness in production (measurement 15.08.2026, 7 days):

* ``E2E match-all`` — 813 messages, ``Telegram onboarding`` — 813: service ones;
* ``CLIENT_PROFILE`` — 470 messages to a real client, although his profile
  is named “IT” but has no filters at all: no keywords, no CPV, no
  regions. For him the product is a mirror of the portal, not a selection.

Now the mode is explicit:

* ``match_all = true``  → “the whole flow”, topic filters are not required;
* ``match_all = false`` and no topic set → the profile matches NOTHING
  (``STAGE_A_SQL``), the client is shown an invitation to configure the filters.

‼️ The BACKFILL is mandatory and is done over ALL profiles without a topic, including
inactive ones. Otherwise the fix would silently switch off the mailing to a live subscriber
(``CLIENT_PROFILE``), and the deactivated ``E2E match-all``, after reactivation by the
``.e2e_request`` hook, would stop receiving tenders and would break the deploy check.
The ``is_active`` axis has nothing to do with it — it answers a different question.

“Topic” = keywords OR classifications: regions, amounts, procedures and languages
only NARROW the selection, but on their own leave the profile a mirror of the portal —
exactly why ``Technical support`` with seven regions and zero
keywords falls into the backfill.

Revision ID: 0043
Revises: 0042
Create Date: 2026-08-15
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0043"
down_revision: Union[str, None] = "0042"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# A profile “without a topic”: neither keywords nor classifications. One predicate per
# meaning — the same text is reused by the guard in tests/db.
NO_TOPIC_SQL = """
    jsonb_array_length(COALESCE(keywords_include, '[]'::jsonb)) = 0
    AND jsonb_array_length(COALESCE(classifications->'include', '[]'::jsonb)) = 0
"""


def upgrade() -> None:
    op.add_column(
        "profiles",
        sa.Column(
            "match_all",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )
    # We preserve TODAY’s behavior: everything that showed the whole flow by
    # emptiness keeps showing it — but now explicitly.
    op.execute(f"""
        UPDATE profiles
           SET match_all = true
         WHERE deleted_at IS NULL
           AND ({NO_TOPIC_SQL})
        """)


def downgrade() -> None:
    op.drop_column("profiles", "match_all")
