"""ORM models for Tender Bot Serbia (DB_SCHEMA.md v1.1 + auth_tokens).

Importing this package registers every model on :data:`src.db.base.Base.metadata`,
which Alembic consumes as ``target_metadata``.
"""

from __future__ import annotations

from src.db.base import Base
from src.db.models.ai import AiUsage, TenderAiAnalysis
from src.db.models.auth import AuthToken
from src.db.models.billing import BillingSubscription, BillingWebhookEvent
from src.db.models.channel_dead_notice_log import ChannelDeadNoticeLog
from src.db.models.delivery import DeliveryAttempt, DeliveryOutbox
from src.db.models.geo import (
    Classification,
    ClassificationSystem,
    Country,
    Region,
)
from src.db.models.profile import Profile, ProfileRevision
from src.db.models.template_library import TemplateLibrary
from src.db.models.reminder_log import ReminderLog
from src.db.models.source import Source
from src.db.models.summary_log import SummaryLog
from src.db.models.tender import CanonicalTender, TenderNormalized, TenderRaw
from src.db.models.tender_award import TenderAward
from src.db.models.award_watch import AwardWatchSubscription, AwardWatchSent
from src.db.models.tender_status import TenderUserStatus
from src.db.models.workflow import FolderItem, TenderFolder, TenderNote
from src.db.models.tenant import (
    AntiAbuseIdentifier,
    Subscription,
    Tenant,
    User,
    UserChannel,
)

__all__ = [
    "Base",
    # geo / reference
    "Country",
    "ClassificationSystem",
    "Classification",
    "Region",
    # tenant / account
    "Tenant",
    "User",
    "UserChannel",
    "Subscription",
    "AntiAbuseIdentifier",
    # profiles
    "Profile",
    "ProfileRevision",
    # template library
    "TemplateLibrary",
    # sources
    "Source",
    # tenders
    "TenderRaw",
    "TenderNormalized",
    "CanonicalTender",
    # tender awards (VG-05, migration 0018)
    "TenderAward",
    # award watch subscriptions + dedup (VG-06 E3b, migration 0019)
    "AwardWatchSubscription",
    "AwardWatchSent",
    # tender user status (funnel)
    "TenderUserStatus",
    # workflow / retention (VG-17, migration 0029)
    "TenderFolder",
    "FolderItem",
    "TenderNote",
    # delivery
    "DeliveryOutbox",
    "DeliveryAttempt",
    # reminder log (deadline reminders idempotency journal)
    "ReminderLog",
    # summary log (daily summary idempotency journal)
    "SummaryLog",
    # channel dead notice log (the channel dropped: incidents, migration 0031)
    "ChannelDeadNoticeLog",
    # ai
    "TenderAiAnalysis",
    "AiUsage",
    # auth
    "AuthToken",
    # billing (VG-11.1, migration 0028)
    "BillingSubscription",
    "BillingWebhookEvent",
]
