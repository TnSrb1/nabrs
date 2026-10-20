"""Cabinet profile service (ARCHITECTURE §12, DB_SCHEMA §2.6/§2.7).

Full CRUD with the plan gate (:mod:`src.api.tiers`), snapshots of ``profile_revisions`` and RLS (``set_tenant`` + an explicit ``WHERE tenant_id``).

The plan is taken from the tenant: trial → Pro rights (ARCHITECTURE §4). The profile limit and field availability are re-checked on the backend (the UI is not the only point).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, time, timezone

from sqlalchemy import func, select

from src.api import repo, tiers
from src.api.cabinet import channel_service
from src.db.models import Profile, ProfileRevision, Tenant
from src.domain.sources import apply_source_selection, validate_source_keys

# Profile fields managed by the form (to copy from the gated payload).
_PROFILE_FIELDS = (
    "name",
    "target_language",
    "is_active",
    "debug_match",
    "keyword_dict",
    "match_all",
    "enabled_source_keys",
    "keywords_include",
    "keywords_exclude",
    "classifications",
    "regions",
    "min_amount_eur",
    "max_amount_eur",
    "include_no_amount",
    "procedure_types",
    "contract_types",
    "customers_include",
    "customers_exclude",
    "languages",
    "min_days_until_deadline",
    "ai_analysis_level",
    "ai_focus_questions",
    "ai_extra_context",
    "delivery_channels",
    "delivery_schedule",
    "digest_cron",
    "quiet_hours_start",
    "quiet_hours_end",
)
_TIME_FIELDS = ("quiet_hours_start", "quiet_hours_end")

# Country of a new profile. It is also the country for the customer suggestions in the new-profile form
# (routes_profiles.customer_suggest_route): for a saved one — its column.
NEW_PROFILE_COUNTRY = "RS"

# A refusal to enable a template over the plan limit: a flag in the query, the banner — in the place
# the request came from (``/app`` or ``/cabinet/account``). A shared place for
# routes_profiles (sets the flag on /activate) and routes_account (reads the flag for the
# banner) — a single point of truth, not matching strings in two routes.
PROFILE_LIMIT_FLAG = "limit"
PROFILE_LIMIT_VALUE = "profiles"

# A profile without a single source matches NOTHING (stage A requires
# ``:source_key = ANY(enabled_source_keys)``) — it cannot be saved (SRC-0b).
# A cabinet catalog key; the form shows it at the platforms block, not only at the top.
# The list also becomes empty after filtering out forged keys
# (:func:`validate_source_keys`), so the check is AFTER the filtering, in the service.
SOURCES_REQUIRED_ERROR = "err_sources_required"


@dataclass
class SaveResult:
    profile: Profile | None = None
    errors: list[str] = field(default_factory=list)
    suppressed: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.profile is not None and not self.errors


def _parse_time(value) -> time | None:
    """``"HH:MM"`` / ``"HH:MM:SS"`` → ``datetime.time``; otherwise None."""
    if value is None or value == "":
        return None
    if isinstance(value, time):
        return value
    parts = str(value).split(":")
    try:
        h = int(parts[0])
        m = int(parts[1]) if len(parts) > 1 else 0
        s = int(parts[2]) if len(parts) > 2 else 0
        return time(hour=h, minute=m, second=s)
    except (ValueError, IndexError):
        return None


def _snapshot(payload: dict) -> dict:
    """JSON-serialisable snapshot for profile_revisions (time → string)."""
    snap = {}
    for key in _PROFILE_FIELDS:
        val = payload.get(key)
        if isinstance(val, time):
            val = val.strftime("%H:%M")
        snap[key] = val
    # we guarantee serializability
    json.dumps(snap, default=str, ensure_ascii=False)
    return snap


async def _tenant_tier(session, tenant_id: str) -> str:
    tenant = await session.get(Tenant, tenant_id)
    if tenant is None:
        return "lite"
    return tiers.effective_tier(tenant.status, tenant.tier)


async def _email_channel_live(session, tenant_id: str, tier: str) -> bool:
    """Whether the tenant’s email is alive — a fact for :func:`tiers.gate_profile` (the Lite branch).

    The same source of truth as :func:`routes_profiles._live_delivery_channels` (``channel_service.email_channel_state``, the ``state.is_live`` flag) — not a second predicate. For plans where the gate does not look at this flag, the SELECT is not made (the default ``True`` in :func:`tiers.gate_profile` is neutral for them).
    """
    if tier != "lite":
        return True
    state = await channel_service.email_channel_state(session, tenant_id)
    return state is not None and state.is_live


async def get_tenant_tier(session, tenant_id: str) -> str:
    """The effective plan of the tenant — for reflecting the field gate in the UI (read-only).

    A thin wrapper over :func:`_tenant_tier`; it does not change the gate’s behavior, it is needed by the forms to show unavailable fields with the note “available on Pro”.
    """
    await repo.set_tenant(session, tenant_id)
    return await _tenant_tier(session, tenant_id)


async def count_active_profiles(session, tenant_id: str) -> int:
    """How many non-deleted profiles the tenant has — for the form’s micro-onboarding."""
    await repo.set_tenant(session, tenant_id)
    return await _count_profiles(session, tenant_id)


async def list_profiles(session, tenant_id: str) -> list[Profile]:
    """All non-deleted profiles of the tenant (RLS + an explicit filter)."""
    await repo.set_tenant(session, tenant_id)
    result = await session.execute(
        select(Profile)
        .where(Profile.tenant_id == tenant_id, Profile.deleted_at.is_(None))
        .order_by(Profile.created_at.desc())
    )
    return list(result.scalars().all())


async def get_profile(session, tenant_id: str, profile_id: str) -> Profile | None:
    """A profile by id, isolated by tenant."""
    await repo.set_tenant(session, tenant_id)
    result = await session.execute(
        select(Profile).where(
            Profile.id == profile_id,
            Profile.tenant_id == tenant_id,
            Profile.deleted_at.is_(None),
        )
    )
    return result.scalars().first()


async def _count_profiles(session, tenant_id: str) -> int:
    result = await session.execute(
        select(func.count())
        .select_from(Profile)
        .where(Profile.tenant_id == tenant_id, Profile.deleted_at.is_(None))
    )
    return int(result.scalar_one() or 0)


def _apply_payload(profile: Profile, clean: dict) -> None:
    for key in _PROFILE_FIELDS:
        if key not in clean:
            continue
        value = clean[key]
        if key == "enabled_source_keys":
            # The platform selection is written together with the subscription moment (SRC-0a, R5):
            # for a newly added source — “now”, for the remaining ones — the previous moment.
            apply_source_selection(profile, value or [], datetime.now(timezone.utc))
            continue
        if key in _TIME_FIELDS:
            value = _parse_time(value)
        setattr(profile, key, value)


async def create_profile(
    session,
    *,
    tenant_id: str,
    user_id: str | None,
    raw_payload: dict,
    name_prefix: str = "Profil",
) -> SaveResult:
    """Creates a profile: limit by plan → field gate → record + revision.

    The profile name is NOT required (the complaint “a template must not have anything mandatory”): empty → auto-generated ``{name_prefix} {N}``, where N is the ordinal number of the tenant’s profile. ``name_prefix`` is localised (passed by the route according to the interface locale — the service does not know the locale; the two i18n axes are not mixed).
    """
    await repo.set_tenant(session, tenant_id)
    tier = await _tenant_tier(session, tenant_id)

    count = await _count_profiles(session, tenant_id)
    if not tiers.can_create_profile(tier, count):
        # a KEY of the cabinet chrome catalog (the template resolves it via ``t``): the service does not know the
        # locale. The concrete limit is not put into the text — it depends on the plan, and without a
        # runtime parameter the message stays one self-contained key.
        return SaveResult(errors=["err_profile_limit"])

    if not (raw_payload.get("name") or "").strip():
        raw_payload = {**raw_payload, "name": f"{name_prefix} {count + 1}"}

    # A template without a channel delivers NOTHING (write_outbox skips it), and the form
    # pre-fills the checkboxes with live channels — empty arrives only if they were unchecked
    # or the POST has no field. If the tenant has a live channel → we take it: delivery is better
    # than silence. Only on CREATION: an edit with an empty selection is a
    # deliberate action, a warning stays there (_delivery_warning.html).
    # Further on, gate_profile itself strips Telegram for Lite.
    if not raw_payload.get("delivery_channels"):
        live = await channel_service.live_delivery_channels(session, tenant_id)
        if live:
            raw_payload = {**raw_payload, "delivery_channels": live}
    email_live = await _email_channel_live(session, tenant_id, tier)
    clean, suppressed = tiers.gate_profile(
        tier, raw_payload, email_channel_live=email_live
    )
    if "enabled_source_keys" in clean:
        clean["enabled_source_keys"] = await validate_source_keys(
            session, clean["enabled_source_keys"] or []
        )
        if not clean["enabled_source_keys"]:
            return SaveResult(errors=[SOURCES_REQUIRED_ERROR])
    profile = Profile(
        tenant_id=tenant_id,
        country_code=NEW_PROFILE_COUNTRY,
        created_by_user_id=user_id,
        version=1,
    )
    _apply_payload(profile, clean)
    session.add(profile)
    await session.flush()

    session.add(
        ProfileRevision(
            profile_id=profile.id,
            version=1,
            snapshot=_snapshot(clean),
            changed_by_user_id=user_id,
            changed_at=datetime.now(timezone.utc),
        )
    )
    await session.commit()
    return SaveResult(profile=profile, suppressed=suppressed)


async def update_profile(
    session,
    *,
    tenant_id: str,
    profile_id: str,
    user_id: str | None,
    raw_payload: dict,
) -> SaveResult:
    """Updates the profile in place: gate (incl. shutting off on downgrade) + revision."""
    await repo.set_tenant(session, tenant_id)
    result = await session.execute(
        select(Profile).where(
            Profile.id == profile_id,
            Profile.tenant_id == tenant_id,
            Profile.deleted_at.is_(None),
        )
    )
    profile = result.scalars().first()
    if profile is None:
        return SaveResult(errors=["err_profile_not_found"])

    tier = await _tenant_tier(session, tenant_id)
    # An empty name on edit does NOT overwrite the existing one (the name is not
    # mandatory, but a profile always has an identifier since its creation).
    # We substitute the current name, so that both the object and the revision snapshot stay correct.
    if not (raw_payload.get("name") or "").strip():
        raw_payload = {**raw_payload, "name": profile.name}
    email_live = await _email_channel_live(session, tenant_id, tier)
    clean, suppressed = tiers.gate_profile(
        tier, raw_payload, email_channel_live=email_live
    )
    if "enabled_source_keys" in clean:
        # A source already in the profile stays valid even if it was
        # temporarily switched off: otherwise editing the name would wipe the subscription.
        clean["enabled_source_keys"] = await validate_source_keys(
            session,
            clean["enabled_source_keys"] or [],
            keep=profile.enabled_source_keys or [],
        )
        if not clean["enabled_source_keys"]:
            return SaveResult(errors=[SOURCES_REQUIRED_ERROR])
    _apply_payload(profile, clean)
    new_version = (profile.version or 1) + 1
    profile.version = new_version

    session.add(
        ProfileRevision(
            profile_id=profile.id,
            version=new_version,
            snapshot=_snapshot(clean),
            changed_by_user_id=user_id,
            changed_at=datetime.now(timezone.utc),
        )
    )
    await session.commit()
    return SaveResult(profile=profile, suppressed=suppressed)


async def soft_delete_profile(session, *, tenant_id: str, profile_id: str) -> bool:
    """Soft delete (``deleted_at``); the history is not touched."""
    await repo.set_tenant(session, tenant_id)
    result = await session.execute(
        select(Profile).where(
            Profile.id == profile_id,
            Profile.tenant_id == tenant_id,
            Profile.deleted_at.is_(None),
        )
    )
    profile = result.scalars().first()
    if profile is None:
        return False
    profile.deleted_at = datetime.now(timezone.utc)
    profile.is_active = False
    await session.commit()
    return True


async def set_profile_active(
    session, *, tenant_id: str, profile_id: str, is_active: bool
) -> bool:
    """Enables / disables a template (``is_active``), WITHOUT deleting it.

    A pair to :func:`soft_delete_profile` by mechanics (the same WHERE: id + tenant + ``deleted_at IS NULL``), but in meaning it is ANOTHER axis: ``soft_delete_profile`` sets ``deleted_at``, after which the template disappears from :func:`list_profiles` and there is nothing left to restore it with from the cabinet. The “Disable / Enable” toggle must be reversible, so it touches only ``is_active``.

    Isolation: the tenant comes from the session and sits in the WHERE alongside RLS — a foreign (or nonexistent) ``profile_id`` finds no row and returns False, changing nothing.
    """
    await repo.set_tenant(session, tenant_id)
    result = await session.execute(
        select(Profile).where(
            Profile.id == profile_id,
            Profile.tenant_id == tenant_id,
            Profile.deleted_at.is_(None),
        )
    )
    profile = result.scalars().first()
    if profile is None:
        return False
    profile.is_active = is_active
    await session.commit()
    return True


async def count_enabled_profiles(session, tenant_id: str) -> int:
    """How many templates are ACTIVE now (``is_active``), not counting deleted ones.

    ‼️ This is not the same as :func:`count_active_profiles` above: that one (despite its name) counts ALL non-deleted rows — the limit axis for CREATION. Here it is the enable axis: after a plan downgrade there may be more non-deleted templates than the limit, and the toggle must keep exactly ``profile_limit`` of them running.
    """
    await repo.set_tenant(session, tenant_id)
    result = await session.execute(
        select(func.count())
        .select_from(Profile)
        .where(
            Profile.tenant_id == tenant_id,
            Profile.deleted_at.is_(None),
            Profile.is_active.is_(True),
        )
    )
    return int(result.scalar_one() or 0)


async def downgrade_notice(session, tenant_id: str, profile: Profile) -> list[str]:
    """Which profile fields are not available on the current plan (for the banner, §12.4)."""
    tier = await _tenant_tier(session, tenant_id)
    payload = {key: getattr(profile, key, None) for key in _PROFILE_FIELDS}
    payload["quiet_hours_start"] = (
        payload["quiet_hours_start"].strftime("%H:%M")
        if isinstance(payload["quiet_hours_start"], time)
        else payload["quiet_hours_start"]
    )
    payload["quiet_hours_end"] = (
        payload["quiet_hours_end"].strftime("%H:%M")
        if isinstance(payload["quiet_hours_end"], time)
        else payload["quiet_hours_end"]
    )
    _, suppressed = tiers.gate_profile(tier, payload)
    return suppressed
