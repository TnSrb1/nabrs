"""Best-effort notification of platform operators by email.

Three events (an access request from the cabinet, an upgrade request from the showcase, a new
registration) must reach the operator not only in Telegram and the cabinet admin, but
also by email. Recipients — the allowlist that gates admin access
(:func:`src.api.admin_access.parse_allowlist` ← ``CABINET_ADMIN_EMAILS``),
plus the operator’s mailbox ``ENTERPRISE_CONTACT_EMAIL`` (info@nab.rs). The mailbox — only for
emails; it does NOT let into the admin: ``is_platform_admin`` reads one allowlist.

The language of the emails — Russian literals: the admin is operator-facing and out of i18n scope
(``CLAUDE.md`` §i18n). The client catalogs (``i18n_email``/``i18n_common``/
``cabinet.i18n``) have nothing to do with it here.

‼️ PII (defect #347): ``smtplib.SMTPRecipientsRefused`` puts the recipient’s address
into the exception arguments, therefore NO ``logger.exception``/``exc_info=True`` —
only the error class (``type(exc).__name__``) through ``logger.warning``.
"""

from __future__ import annotations

import logging

from src.api import admin_access
from src.api.config import CabinetSettings
from src.api.emailer import EmailSender
from src.api.security import normalize_email
from src.plans.catalog import ENTERPRISE_CONTACT_EMAIL

logger = logging.getLogger(__name__)


async def notify_admins_email(
    *,
    email_sender: EmailSender,
    settings: CabinetSettings,
    subject: str,
    text_body: str,
) -> int:
    """Sends an email to each operator from the allowlist. Returns the number of successes.

    Best-effort: a failure for a SPECIFIC address is logged (only the error class, without
    the address and without a traceback — PII #347) and does not interrupt the mailing to the others; the
    exception is not raised outward (the registration/request route must not fall over because of
    the mail). Recipients — allowlist ∪ ``ENTERPRISE_CONTACT_EMAIL``, deduplicated by the
    normalized address. An empty allowlist does not disable the mailing: the email
    still goes to info@nab.rs.
    """
    recipients = admin_access.parse_allowlist(settings) | {
        normalize_email(ENTERPRISE_CONTACT_EMAIL)
    }
    sent = 0
    for addr in sorted(recipients):
        try:
            await email_sender.send(to=addr, subject=subject, text_body=text_body)
            sent += 1
        except Exception as exc:  # noqa: BLE001 — best-effort, no PII in logs
            logger.warning(
                "notify_admins_email: email to the operator not sent (%s)",
                type(exc).__name__,
            )
    return sent
