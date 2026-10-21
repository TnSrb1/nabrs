"""Channel Dead Notify Worker — an e-mail to the subscriber about a dropped delivery channel.

A delivery channel dies silently: the user blocked the bot, unlinked the binding, or it was normalized by hand (a real case — the profile ``7986f3b0``, the channel has not been live since ~22.06, 2683 dead deliveries, the user was not notified at all). The profile meanwhile stays active, the tenders match, but there is nowhere to send them — from the user's side this looks like “the service broke”. #340 closed the technical part (dead records are no longer written to ``delivery_outbox``); this worker closes the human part.

‼️ **The notification goes ONLY by e-mail** — by principle, not by poverty: notifying through a broken channel is pointless (if Telegram dropped → there is nowhere to send to in Telegram). The e-mail exists for every user (the magic-link login goes through it too) and does not depend on Telegram. The second, independent path — a banner in the cabinet (``dashboard.html``, in case the e-mail goes to spam).

``delivery_outbox`` is tender-centric (the PK semantics profile×tender×channel) — the “channel dropped” notification does not fit there: it is not about a tender. Therefore a standalone worker + a separate compose service (the model — :mod:`src.workers.subscription_reminders`).

What counts as “dropped” (:func:`src.workers.matching.channel_states` — THE SAME predicate as routing; a second one must not be written, the selections would diverge):

* the channel is configured in an active profile (``profiles.delivery_channels``);
* the tenant HAS a ``user_channels`` row for this channel (they once connected it);
* but the freshest row is not live: ``status`` ``blocked``/``revoked`` or an empty ``external_id``.

‼️ A channel the tenant does not have AT ALL (``user_channels`` is empty) is “not set up yet” (onboarding not passed), and NOT “dropped”. We do not write to such a user: onboarding is handled by the checklist on the dashboard. That is exactly why ``channel_states`` is needed (it keeps “the channel was set up”), and not ``live_channels`` (it collapses “dropped” and “never existed” into one “not in the set”).

Idempotency — claim-then-send through ``channel_dead_notice_log``
(migration 0031), the model “an open incident”:

1. **One e-mail per one incident.** A partial UNIQUE ``(tenant_id, channel)
   WHERE resolved_at IS NULL`` → at most one open incident per pair.
   Claim = ``INSERT ... ON CONFLICT DO NOTHING RETURNING id``; the e-mail goes out
   only on a non-zero RETURNING. The channel is still dead on the next cycle →
   the claim does not pass → there is NO second e-mail (the user must not be spammed).
2. **Recovery closes the incident.** The channel is live again → ``resolved_at =
   now()``. It drops again → there are no open ones → a new claim → a new e-mail.
3. **A channel failure does NOT roll the log back.** The order: claim (a short transaction,
   commit), then send — as in reminders/subscription_reminders. A rollback would return
   the claim and loop the sending every cycle.
4. **No valid e-mail — we do not claim.** The address check goes BEFORE the claim (as
   in ``subscription_reminders``): an incident opened without the ability to send would block
   all the next cycles, and the user would not get the e-mail even after the address is fixed.
   Not claimed → the address is fixed → the e-mail goes out.

The worker goes to the DB **privileged** (without ``SET app.current_tenant_id``): a cross-tenant server operation, the owner bypasses RLS (ARCHITECTURE §8) — the same contour as ``subscription_reminders``/``reminders``.

Privacy: the logs get only ``tenant_id``/``channel``. The subscriber's e-mail is NEVER logged (neither in INFO nor in exception) — PII.

The dependencies are injected (the e-mail sender, the session factory, the interval, the base URL of the cabinet) — unit tests without a real PostgreSQL/network (ADR-013).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from typing import Any, Callable

from sqlalchemy import text

from src.api.emailer import EmailSender, MockEmailSender
from src.db.session import session_scope
from src.i18n_email import email_t
from src.workers.matching import channel_states

logger = logging.getLogger(__name__)

DEFAULT_CABINET_BASE_URL = "https://nab.rs"

# The “My Telegram” page in the cabinet: the binding status + a reconnect button.
RECONNECT_PATH = "/cabinet/telegram"

# Human-readable channel names for the e-mail (the cabinet/e-mails — ru only).
CHANNEL_LABELS = {"telegram": "Telegram"}

# Tenants with an active profile: the same activity conditions as in matching
# STAGE_A_SQL (profiles.is_active + the tenant status + access not expired) — otherwise
# we would write to users to whom nothing is sent anyway.
#
# EXACTLY ONE row per tenant (`DISTINCT ON (ten.id)`, as in
# subscription_reminders `_TENANTS_SQL`). The incident is put on (tenant, channel),
# and not on the user: the channel is common to the tenant. Without DISTINCT ON a tenant with several
# users would give N rows, and the e-mail would go to a RANDOM one of them (there would be no spam
# — the claim would cut off the others — but the addressee would depend on the order of the DB output).
#
# The addressee = the oldest user (the account owner), BUT among those to whom one can write at all:
# `ORDER BY ... (address valid) DESC` lifts the suitable ones higher. Otherwise
# one broken address of the owner would silence the whole tenant — no claim is made (see
# poll_once), and the other users with working mail would never learn that
# the delivery had stopped. All addresses broken → the row still comes, and poll_once
# honestly skips the tenant without opening an incident.
#
# delivery_channels collapse into one array over ALL active profiles of the tenant
# (the “where to send” setting of profiles may differ, the channel is common); the aggregate
# is counted in a LATERAL, so as not to mix GROUP BY with DISTINCT ON.
_TENANTS_SQL = text("""
    SELECT DISTINCT ON (ten.id)
           ten.id                    AS tenant_id,
           u.email                   AS email,
           u.email_status            AS email_status,
           u.locale                  AS locale,
           chans.configured_channels AS configured_channels
    FROM tenants ten
    JOIN users u ON u.tenant_id = ten.id
                AND u.deleted_at IS NULL
    JOIN LATERAL (
        SELECT array_agg(DISTINCT ch) AS configured_channels
        FROM profiles p
        CROSS JOIN LATERAL unnest(COALESCE(p.delivery_channels, '{}')) AS ch
        WHERE p.tenant_id = ten.id
          AND p.is_active = true
    ) chans ON chans.configured_channels IS NOT NULL
    WHERE ten.status IN ('trial', 'active')
      AND (ten.access_until IS NULL OR ten.access_until > now())
    ORDER BY ten.id,
             (u.email IS NOT NULL
              AND btrim(u.email) <> ''
              AND u.email_status IS DISTINCT FROM 'bounced') DESC,
             u.created_at,
             u.id
    """)

# Claim: open an incident. ON CONFLICT targets the PARTIAL UNIQUE of 0031 — its predicate
# must be repeated, otherwise Postgres will not infer the index.
_CLAIM_SQL = text("""
    INSERT INTO channel_dead_notice_log (tenant_id, channel)
    VALUES (CAST(:tenant_id AS uuid), CAST(:channel AS text))
    ON CONFLICT (tenant_id, channel) WHERE resolved_at IS NULL DO NOTHING
    RETURNING id
    """)

# The channel is live again → we close the open incident. The next drop will open a
# new one (and the e-mail will go out again). No open one — no-op.
_RESOLVE_SQL = text("""
    UPDATE channel_dead_notice_log
       SET resolved_at = now()
     WHERE tenant_id = CAST(:tenant_id AS uuid)
       AND channel = CAST(:channel AS text)
       AND resolved_at IS NULL
    """)


# The reminder “a template exists, no channel”: THROUGH THIS SAME log, as a row with the pseudo-channel
# :data:`NUDGE_CHANNEL`. The incident is never closed
# (``resolved_at`` is not touched — resolve goes only for the real channels
# telegram/email), therefore the partial UNIQUE gives “once per tenant FOREVER”
# without a new table and a migration.
NUDGE_CHANNEL = "no_channel_nudge"

# How long after the creation of the FIRST active template to remind.
NUDGE_DELAY_HOURS = 24

# ‼️ The upper bound is mandatory: without it the very first run would send an e-mail to ALL
# old tenants who have a template but no channel (the same trap as
# EXPIRED_GRACE_HOURS in subscription_reminders). The reminder is about the fresh
# onboarding, not about abandoned accounts.
NUDGE_MAX_AGE_HOURS = 168

# Tenants whose FIRST active non-deleted template was created in the window
# [MAX_AGE, DELAY] hours ago, the e-mail has not gone out yet, the access is alive. Whether
# the channel is live — is decided by the python predicate :func:`channel_states` (not by a second SQL).
# The addressee is the same choice as in :data:`_TENANTS_SQL` (a suitable address first).
_NUDGE_TENANTS_SQL = text("""
    SELECT DISTINCT ON (ten.id)
           ten.id        AS tenant_id,
           u.email       AS email,
           u.email_status AS email_status,
           u.locale      AS locale
    FROM tenants ten
    JOIN users u ON u.tenant_id = ten.id
                AND u.deleted_at IS NULL
    JOIN LATERAL (
        SELECT min(p.created_at) AS first_active_at
        FROM profiles p
        WHERE p.tenant_id = ten.id
          AND p.is_active = true
          AND p.deleted_at IS NULL
    ) fp ON fp.first_active_at IS NOT NULL
    WHERE ten.status IN ('trial', 'active')
      AND (ten.access_until IS NULL OR ten.access_until > now())
      AND fp.first_active_at <= now() - (CAST(:delay_hours AS int) * INTERVAL '1 hour')
      AND fp.first_active_at > now() - (CAST(:max_age_hours AS int) * INTERVAL '1 hour')
      AND NOT EXISTS (
          SELECT 1 FROM channel_dead_notice_log l
          WHERE l.tenant_id = ten.id AND l.channel = :nudge_channel
      )
    ORDER BY ten.id,
             (u.email IS NOT NULL
              AND btrim(u.email) <> ''
              AND u.email_status IS DISTINCT FROM 'bounced') DESC,
             u.created_at,
             u.id
    """)


def build_nudge_url(cabinet_base_url: str) -> str:
    """A link to the “Channels” section of the “Moj kabinet” hub."""
    return f"{(cabinet_base_url or DEFAULT_CABINET_BASE_URL).rstrip('/')}/cabinet/account#kanali"


def build_reconnect_url(cabinet_base_url: str) -> str:
    """A direct link to the channel reconnection page in the cabinet."""
    return (
        f"{(cabinet_base_url or DEFAULT_CABINET_BASE_URL).rstrip('/')}{RECONNECT_PATH}"
    )


def channel_label(channel: str) -> str:
    """The name of the channel for the e-mail (a proper name, not translated); unknown — as is."""
    return CHANNEL_LABELS.get(channel, channel)


def build_email_subject(channel: str, locale: str | None = None) -> str:
    return email_t(locale, "dch_subject").format(label=channel_label(channel))


def build_email_body(
    channel: str, *, reconnect_url: str, locale: str | None = None
) -> str:
    """The e-mail text in the language of ``users.locale`` (None → sr). The tone is care, not an “error”.

    The meaning: tenders are not coming to you now, here is why, and the link to fix it.
    """
    return email_t(locale, "dch_body").format(
        label=channel_label(channel), url=reconnect_url
    )


class ChannelDeadNotifyWorker:
    """The polling worker of the channel-dropped notifications (e-mail, idempotent)."""

    def __init__(
        self,
        *,
        email_sender: EmailSender | None = None,
        session_factory: Callable[[], Any] = session_scope,
        poll_interval: float = 3600.0,
        cabinet_base_url: str = DEFAULT_CABINET_BASE_URL,
    ) -> None:
        self._email = email_sender or MockEmailSender()
        self._session_factory = session_factory
        self._poll_interval = poll_interval
        self._reconnect_url = build_reconnect_url(cabinet_base_url)
        self._nudge_url = build_nudge_url(cabinet_base_url)
        self._stopped = asyncio.Event()

    # -- public API ----------------------------------------------------- #

    def stop(self) -> None:
        """Asks the worker to finish after the current iteration."""
        self._stopped.set()

    async def run(self) -> None:
        logger.info(
            "channel_dead_notify: polling started (interval=%.0fs)", self._poll_interval
        )
        while not self._stopped.is_set():
            try:
                sent = await self.poll_once()
                sent += await self.poll_nudges_once()
                if sent:
                    logger.info("channel_dead_notify: e-mails sent per cycle: %d", sent)
            except asyncio.CancelledError:
                raise
            except (
                Exception
            ):  # noqa: BLE001 — a cycle failure does not break the worker
                logger.exception("channel_dead_notify: polling cycle error")
            await self._sleep(self._poll_interval)

    async def poll_once(self) -> int:
        """One pass: tenants → channel state → claim/resolve → send."""
        sent = 0
        for row in await self._load_tenants():
            configured = set(row.configured_channels or ())
            if not configured:
                continue
            states = await self._channel_states(row.tenant_id)

            for channel in sorted(configured):
                state = states.get(channel)
                if state is None:
                    # The channel was never connected — this is onboarding, not a drop.
                    continue
                if state.is_live:
                    # It recovered: we close the incident, so that the next drop
                    # gives an e-mail again.
                    await self._resolve(row.tenant_id, channel)
                    continue
                # We check the address BEFORE the claim (as subscription_reminders):
                # a claim without the ability to send would open an incident forever —
                # it blocks all the next cycles, and the user would not get the e-mail even
                # after the address is fixed.
                if not self._can_email(row):
                    logger.warning(
                        "channel_dead_notify: the channel is dead, but there is nowhere to send "
                        "(no valid e-mail) tenant=%s channel=%s",
                        row.tenant_id,
                        channel,
                    )
                    continue
                if not await self._claim(row.tenant_id, channel):
                    continue  # the incident is already open — we do not send a second e-mail
                if await self._send_email(row, channel):
                    sent += 1
        return sent

    async def poll_nudges_once(self) -> int:
        """The reminder “a template exists, no channel”: one e-mail per tenant (see NUDGE_CHANNEL)."""
        sent = 0
        async with self._session_factory() as session:
            rows = list(
                await session.execute(
                    _NUDGE_TENANTS_SQL,
                    {
                        "delay_hours": NUDGE_DELAY_HOURS,
                        "max_age_hours": NUDGE_MAX_AGE_HOURS,
                        "nudge_channel": NUDGE_CHANNEL,
                    },
                )
            )
        for row in rows:
            states = await self._channel_states(row.tenant_id)
            # ‼️ Any user_channels row = the channel was once set up (alive or
            # dropped). For the dropped one — the dead-channel e-mail (poll_once), not
            # “not connected yet”: the nudge only for those who never set up a channel at all.
            if states:
                continue
            if not self._can_email(row):
                continue  # a claim without an address would silence the reminder forever
            if not await self._claim(row.tenant_id, NUDGE_CHANNEL):
                continue
            try:
                await self._email.send(
                    to=row.email,
                    subject=email_t(getattr(row, "locale", None), "nch_subject"),
                    text_body=email_t(getattr(row, "locale", None), "nch_body").format(
                        url=self._nudge_url
                    ),
                )
            except Exception as exc:  # noqa: BLE001 — we do not roll the log back
                # PII: only the error class; the address does not go into the logs.
                logger.error(
                    "channel_dead_notify: nudge e-mail failure tenant=%s error=%s",
                    row.tenant_id,
                    type(exc).__name__,
                )
                continue
            logger.info(
                "channel_dead_notify: channel reminder sent tenant=%s",
                row.tenant_id,
            )
            sent += 1
        return sent

    # -- internal -------------------------------------------------------- #

    @staticmethod
    def _can_email(row: Any) -> bool:
        """Is there anywhere to send: the address is set and not rejected as invalid.

        Mirrors the branch “the address is valid” in the ORDER BY of :data:`_TENANTS_SQL` (it lifts the suitable user up) — the predicates must match.
        """
        return bool((row.email or "").strip()) and row.email_status != "bounced"

    async def _load_tenants(self) -> list[Any]:
        async with self._session_factory() as session:
            result = await session.execute(_TENANTS_SQL)
            return list(result)

    async def _channel_states(self, tenant_id: Any) -> dict[str, Any]:
        """The tenant’s channel state — with the SAME predicate as routing."""
        async with self._session_factory() as session:
            return await channel_states(session, str(tenant_id))

    async def _claim(self, tenant_id: Any, channel: str) -> bool:
        """Atomically opens the incident. True — ours, we send the e-mail."""
        async with self._session_factory() as session:
            result = await session.execute(
                _CLAIM_SQL, {"tenant_id": str(tenant_id), "channel": channel}
            )
            await session.commit()
        return bool(list(result))

    async def _resolve(self, tenant_id: Any, channel: str) -> None:
        """Closes the open incident (the channel is live again). No open one — no-op."""
        async with self._session_factory() as session:
            result = await session.execute(
                _RESOLVE_SQL, {"tenant_id": str(tenant_id), "channel": channel}
            )
            await session.commit()
        if getattr(result, "rowcount", 0):
            logger.info(
                "channel_dead_notify: channel restored tenant=%s channel=%s",
                tenant_id,
                channel,
            )

    async def _send_email(self, row: Any, channel: str) -> bool:
        """Sends the e-mail. The log is NOT rolled back on failure (see the module docstring).

        The addressee has already been checked by :meth:`_can_email` before the claim. The subscriber's address does not get into the logs — PII.
        """
        try:
            await self._email.send(
                to=row.email,
                subject=build_email_subject(channel, getattr(row, "locale", None)),
                text_body=build_email_body(
                    channel,
                    reconnect_url=self._reconnect_url,
                    locale=getattr(row, "locale", None),
                ),
            )
        except (
            Exception
        ) as exc:  # noqa: BLE001 — a channel failure does not roll the log back
            # ‼️ NOT logger.exception and NOT %s of exc: the subscriber’s address is PII, and the
            # the text of the SMTP error contains it (`SMTPRecipientsRefused` puts the
            # recipient right into the arguments, the traceback prints them in full).
            # We log only the CLASS of the error — that is enough for diagnostics.
            logger.error(
                "channel_dead_notify: e-mail failure tenant=%s channel=%s error=%s",
                row.tenant_id,
                channel,
                type(exc).__name__,
            )
            return False
        logger.info(
            "channel_dead_notify: the dropped-channel e-mail sent tenant=%s channel=%s",
            row.tenant_id,
            channel,
        )
        return True

    async def _sleep(self, delay: float) -> None:
        """An interruptible sleep: reacts to stop() while waiting."""
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(self._stopped.wait(), timeout=delay)


async def main() -> None:  # pragma: no cover — entrypoint, not tested
    """Entry point of the dropped-channel notification worker (the container via Compose)."""
    from src.api.emailer import make_email_sender
    from src.log_secrets import install_secret_log_filter

    logging.basicConfig(level=logging.INFO)
    install_secret_log_filter()
    email_sender = make_email_sender(
        smtp_host=os.getenv("SMTP_HOST", ""),
        smtp_port=int(os.getenv("SMTP_PORT", "587")),
        smtp_user=os.getenv("SMTP_USER", ""),
        smtp_password=os.getenv("SMTP_PASSWORD", ""),
        smtp_from=os.getenv("SMTP_FROM", ""),
        postmark_token=os.getenv("POSTMARK_SERVER_TOKEN", ""),
        email_from=os.getenv("EMAIL_FROM", "noreply@nab.rs"),
    )
    worker = ChannelDeadNotifyWorker(
        email_sender=email_sender,
        cabinet_base_url=os.getenv("CABINET_BASE_URL", DEFAULT_CABINET_BASE_URL),
    )
    await worker.run()


if __name__ == "__main__":  # pragma: no cover
    asyncio.run(main())
