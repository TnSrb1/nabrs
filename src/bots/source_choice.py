"""``/izvori`` — the subscriber chooses the profile’s sources in the bot (SRC-0b, R6).

The logic and keyboards are separate from the handlers (:mod:`src.bots.telegram`): here
there is no aiogram router, only the DB session and pure functions — tested on a
stub of the session.

Addressing of the buttons — ``callback_data`` ≤64 bytes (the Telegram limit), ONE UUID:

* ``src:p:<profile_uuid>`` — the choice of the profile (the tenant has several);
* ``src:t:<profile_uuid>:<i>:<tag>`` — the toggle of a source: ``i`` — the index in
  :func:`src.domain.sources.list_sources_for_selection` (the order by
  ``source_key``, deterministic), ``tag`` — 4 hex characters of the key hash. The index
  is short, but the list may shift (the admin turned on a new portal while
  the old keyboard was hanging) — the tag catches such a shift, and pressing a stale
  button does NOT toggle someone else’s source, but asks to open ``/izvori`` again.

The write — only through :func:`src.domain.sources.apply_source_selection`
(the moment of subscription, R5). The profile is looked up under the GUC of the tenant, resolved by
chat_id (``resolve_tenant_id`` = SECURITY DEFINER + ``set_current_tenant``),
plus an explicit ``WHERE tenant_id`` — a foreign ``profile_uuid`` from a forged
callback is not found.
"""

from __future__ import annotations

import hashlib
import html
import re
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy import select

from src.api.cabinet.source_banner import PendingSource
from src.bots.i18n import bot_t
from src.db.models import Profile
from src.domain.sources import (
    SourceInfo,
    apply_source_selection,
    list_sources_for_selection,
    validate_source_keys,
)
from src.i18n_common import common_t, country_label

CB_PROFILE = "src:p:"
CB_TOGGLE = "src:t:"
#: “Turn on in all templates” from the notification: ``src:a:<source_key>`` (the key
#: is short, ASCII — fits into 64 bytes). The write — the same service as the cabinet.
CB_ENABLE_ALL = "src:a:"
_SOURCE_KEY_RE = re.compile(r"[a-z0-9_]{1,40}")

#: The outcomes of the toggle press (the keys of the reply — ``src_toast_<outcome>`` in the catalog).
ADDED, REMOVED, LAST, STALE = "added", "removed", "last", "stale"


def key_tag(source_key: str) -> str:
    """The short fingerprint of the source key for ``callback_data`` (4 hex)."""
    return hashlib.sha1(source_key.encode("utf-8")).hexdigest()[:4]


def profile_callback(profile_id) -> str:
    return f"{CB_PROFILE}{profile_id}"


def toggle_callback(profile_id, index: int, source_key: str) -> str:
    return f"{CB_TOGGLE}{profile_id}:{index}:{key_tag(source_key)}"


def _uuid(raw: str) -> str | None:
    try:
        return str(UUID(raw))
    except (TypeError, ValueError):
        return None


def parse_profile_callback(data: str | None) -> str | None:
    if not data or not data.startswith(CB_PROFILE):
        return None
    return _uuid(data[len(CB_PROFILE) :])


@dataclass(frozen=True)
class ToggleRef:
    profile_id: str
    index: int
    tag: str


def parse_toggle_callback(data: str | None) -> ToggleRef | None:
    if not data or not data.startswith(CB_TOGGLE):
        return None
    parts = data[len(CB_TOGGLE) :].split(":")
    if len(parts) != 3:
        return None
    profile_id = _uuid(parts[0])
    if profile_id is None or not parts[1].isdigit():
        return None
    return ToggleRef(profile_id, int(parts[1]), parts[2])


async def load_profile(session, tenant_id, profile_id) -> Profile | None:
    """The tenant’s profile by id (the GUC is already set by the caller)."""
    result = await session.execute(
        select(Profile).where(
            Profile.id == profile_id,
            Profile.tenant_id == tenant_id,
            Profile.deleted_at.is_(None),
        )
    )
    return result.scalars().first()


def source_label(source: SourceInfo, locale: str | None) -> str:
    """The source label: the name + a mark of the source switched off by the admin (owner 30.09 item 3)."""
    if source.enabled:
        return source.label
    return f"{source.label} ({common_t(locale, 'src_paused')})"


def _countries(source: SourceInfo, locale: str | None) -> str:
    return ", ".join(country_label(locale, c) for c in source.country_codes)


def menu_text(profile, sources: list[SourceInfo], locale: str | None) -> str:
    """The text of the sources screen of one profile: what is selected, the countries, the marks."""
    selected = set(getattr(profile, "enabled_source_keys", None) or ())
    name = profile.name or bot_t(locale, "no_profile_name")
    # parse_mode=HTML: the template name is the subscriber’s input, we escape it.
    lines = [bot_t(locale, "src_menu_title").format(name=html.escape(name)), ""]
    for source in sources:
        mark = "☑" if source.source_key in selected else "☐"
        countries = _countries(source, locale)
        tail = f" — {countries}" if countries else ""
        label = html.escape(source_label(source, locale))
        lines.append(f"{mark} {label}{tail}")
    lines += ["", bot_t(locale, "src_menu_hint")]
    return "\n".join(lines)


def menu_keyboard(
    profile, sources: list[SourceInfo], locale: str | None, *, with_back: bool
) -> InlineKeyboardMarkup:
    selected = set(getattr(profile, "enabled_source_keys", None) or ())
    rows = [
        [
            InlineKeyboardButton(
                text=(
                    f"{'☑' if s.source_key in selected else '☐'} "
                    f"{source_label(s, locale)}"
                ),
                callback_data=toggle_callback(profile.id, i, s.source_key),
            )
        ]
        for i, s in enumerate(sources)
        # A source switched off by the admin can only be REMOVED — we do not offer
        # an unselected one (it cannot be turned on anyway, validate_source_keys).
        if s.enabled or s.source_key in selected
    ]
    if with_back:
        rows.append(
            [
                InlineKeyboardButton(
                    text=bot_t(locale, "src_back_btn"), callback_data="src:l"
                )
            ]
        )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def profiles_keyboard(profiles, locale: str | None) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=(p.name or bot_t(locale, "no_profile_name"))[:60],
                    callback_data=profile_callback(p.id),
                )
            ]
            for p in profiles
        ]
    )


async def toggle_source(
    session, profile, ref: ToggleRef, now: datetime
) -> tuple[str, SourceInfo | None, list[SourceInfo]]:
    """Toggle the source ``ref`` of ``profile`` and commit.

    Returns ``(outcome, source, the current list of sources)``. The last source cannot be removed
    (``LAST``): a profile without a source gets nothing — the same refusal as in the cabinet
    form. A stale button (an index outside the list, the tag did not match, an unselected
    switched-off source) — ``STALE``, without a write.
    """
    current = list(getattr(profile, "enabled_source_keys", None) or [])
    sources = await list_sources_for_selection(session, current)
    if not 0 <= ref.index < len(sources):
        return STALE, None, sources
    source = sources[ref.index]
    if key_tag(source.source_key) != ref.tag:
        return STALE, None, sources
    key = source.source_key
    if key in current:
        new_keys = [k for k in current if k != key]
        if not new_keys:
            return LAST, source, sources
        outcome = REMOVED
    else:
        if not source.enabled:
            return STALE, None, sources
        new_keys = current + [key]
        outcome = ADDED
    new_keys = await validate_source_keys(session, new_keys, keep=current)
    apply_source_selection(profile, new_keys, now)
    await session.commit()
    return outcome, source, sources


# --- The notification “turn on in all templates” (step 4 of the brief) -------------------- #


def enable_all_callback(source_key: str) -> str:
    return f"{CB_ENABLE_ALL}{source_key}"


def parse_enable_all_callback(data: str | None) -> str | None:
    """The source key from the callback “in all templates”; garbage → ``None``."""
    if not data or not data.startswith(CB_ENABLE_ALL):
        return None
    key = data[len(CB_ENABLE_ALL) :]
    return key if _SOURCE_KEY_RE.fullmatch(key) else None


def everywhere_notice(
    pending: PendingSource, *, profile_total: int, locale: str | None
) -> tuple[str, InlineKeyboardMarkup]:
    """One message: the gist of the cabinet banner + the button “turn on in all templates”.

    The text is the same as the dashboard banner (the source, what it gives, the plan / match_all), in the
    language of the channel. The source name — from the registry, escaped (parse_mode=HTML).
    """
    lines = [
        bot_t(locale, "src_banner_title").format(name=html.escape(pending.label)),
        "",
        bot_t(locale, "src_banner_body").format(
            missing=pending.missing_in, total=profile_total
        ),
    ]
    if pending.is_plan:
        lines.append(bot_t(locale, "src_banner_plan"))
    if pending.has_match_all:
        lines.append(bot_t(locale, "src_banner_match_all"))
    markup = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=bot_t(locale, "src_banner_enable"),
                    callback_data=enable_all_callback(pending.source_key),
                )
            ]
        ]
    )
    return "\n".join(lines), markup
