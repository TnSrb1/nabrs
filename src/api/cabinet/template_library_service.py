"""Service of the search-template library (EPIK 4 + EPIK 5).

EPIK 4: reading the public approved templates.
EPIK 5: propose_from_profile / list_pending / approve_template / reject_template / update_pending.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy import select

from src.db.models.profile import Profile
from src.db.models.template_library import TemplateLibrary
from src.domain.text_norm import RU_ONLY_LETTERS, cyr_to_lat, fold

logger = logging.getLogger(__name__)


async def list_public_templates(db, country_code: str = "RS") -> list[TemplateLibrary]:
    """Returns all public approved templates, sorted by (activity_kind, name)."""
    result = await db.execute(
        select(TemplateLibrary)
        .where(
            TemplateLibrary.is_public.is_(True),
            TemplateLibrary.status == "approved",
            TemplateLibrary.country_code == country_code,
        )
        .order_by(TemplateLibrary.activity_kind, TemplateLibrary.name)
    )
    return list(result.scalars().all())


def localized_view(tpl: TemplateLibrary, lang: str) -> dict:
    """The showcase template in the cabinet’s interface language: texts + keyword tags.

    The texts are taken from ``tpl.i18n[lang]`` (0036); if there is no translation, the original is returned (fail-open, like ``okrug_label`` / ``status_label``): the user templates of EPIK 5 have ``i18n IS NULL``, and their text must not be machine-translated.

    ‼️ The tags are SEARCH terms, not labels: showing a Serbian reader the Russian word for “cleaning” when the matching searches for “čišćenje” would be lying about how the search works. Therefore outside ru we take the first Serbian form from the same dictionary that the matching expands with (:func:`src.workers.keyword_expand.expand_term`, a mirror of stage B) — one predicate per meaning, not a second list of forms. No form → the term as is.

    Returns a dict, not an ORM object: the template accesses the same field names (Jinja does item lookup for a dict), so the markup does not change.
    """
    texts = (getattr(tpl, "i18n", None) or {}).get(lang) or {}
    if lang == "sr":
        # The whole sr UI is in Latin, but the 0036 translations are written in Cyrillic: at render time
        # we transliterate them into Latin with ONE function (the data is not changed). Only what is
        # taken from the sr translation: the Russian fallback in Cyrillic is not touched.
        texts = {k: _sr_latin(v) for k, v in texts.items()}
    return {
        "id": tpl.id,
        "name": texts.get("name") or tpl.name,
        "description": texts.get("description") or tpl.description,
        "activity_kind": texts.get("activity_kind") or tpl.activity_kind,
        "keywords_include": _localized_keywords(tpl.keywords_include, lang),
        "classifications": tpl.classifications,
    }


def _sr_latin(value):
    """Serbian text → Latin; a non-string or Russian text — as is."""
    if not isinstance(value, str) or any(ch in RU_ONLY_LETTERS for ch in value):
        return value
    return cyr_to_lat(value)


def _localized_keywords(keywords_include, lang: str):
    """The terms of the ``all_of`` groups in the SEARCH language (see :func:`localized_view`)."""
    if not keywords_include or lang == "ru":
        return keywords_include

    from src.workers.keyword_expand import expand_term

    out = []
    for group in keywords_include or []:
        terms = (group or {}).get("all_of") or []
        shown = []
        for term in terms:
            forms = [f for f in expand_term(term) if f != term]
            shown.append(_sr_latin(forms[0] if forms else term))
        out.append({**group, "all_of": shown})
    return out


#: The similarity threshold of the package and template names (the share of common words in the union).
_DUP_THRESHOLD = 0.8
#: A word longer than that is cut to a prefix: “medicinska” / “medicinske” is one word.
_DUP_STEM = 8


def _name_words(name: str | None) -> frozenset[str]:
    """: The name’s words without parenthetical notes: folding + the prefix ``_DUP_STEM``."""
    text = fold(name)
    while "(" in text and ")" in text:
        a, b = text.index("("), text.index(")")
        if a > b:
            break
        text = text[:a] + " " + text[b + 1 :]
    words = "".join(c if c.isalnum() else " " for c in text).split()
    return frozenset(w[:_DUP_STEM] for w in words if len(w) > 2)


def names_duplicate(a: str | None, b: str | None) -> bool:
    """: The names are “essentially one”: the folded ones are equal, or the common words are ≥ 80% of the union."""
    wa, wb = _name_words(a), _name_words(b)
    if not wa or not wb:
        return False
    return len(wa & wb) / len(wa | wb) >= _DUP_THRESHOLD


def duplicates_of_packages(tpl: TemplateLibrary, packages) -> bool:
    """A template duplicates the package ``vertical_packages``: the names matched in at least one language (sr / ru / en) — the decision does not depend on the interface language, just like the layout."""
    names: list[str | None] = [tpl.name]
    names += [
        (v or {}).get("name") for v in (getattr(tpl, "i18n", None) or {}).values()
    ]
    for pkg in packages:
        for pname in pkg.name.values():
            if any(names_duplicate(pname, n) for n in names):
                return True
    return False


async def get_template(db, template_id: str) -> TemplateLibrary | None:
    """Loads a template by id. Returns None if not found."""
    try:
        result = await db.execute(
            select(TemplateLibrary).where(
                TemplateLibrary.id == template_id,
                TemplateLibrary.is_public.is_(True),
                TemplateLibrary.status == "approved",
            )
        )
        return result.scalar_one_or_none()
    except Exception:
        return None


# --------------------------------------------------------------------------- #
# EPIK 5 — user suggestions + moderation
# --------------------------------------------------------------------------- #


async def propose_from_profile(
    db,
    *,
    user_id: str,
    tenant_id: str,
    profile_id: str,
    name: str,
    description: str | None = None,
) -> TemplateLibrary | None:
    """Creates a pending record in template_library from the user’s profile.

    Checks that the profile belongs to tenant_id. Returns None if the profile is not found or does not belong to the tenant.
    """
    result = await db.execute(
        select(Profile).where(
            Profile.id == profile_id,
            Profile.tenant_id == tenant_id,
            Profile.deleted_at.is_(None),
        )
    )
    profile = result.scalar_one_or_none()
    if profile is None:
        return None

    tmpl = TemplateLibrary(
        name=name,
        description=description,
        is_public=False,
        status="pending",
        created_by_user_id=user_id,
        country_code="RS",
        keywords_include=profile.keywords_include,
        classifications=profile.classifications,
        activity_kind=None,
    )
    db.add(tmpl)
    await db.commit()
    return tmpl


async def list_pending(db, country_code: str = "RS") -> list[TemplateLibrary]:
    """Returns all pending templates, sorted by created_at DESC."""
    result = await db.execute(
        select(TemplateLibrary)
        .where(
            TemplateLibrary.status == "pending",
            TemplateLibrary.country_code == country_code,
        )
        .order_by(TemplateLibrary.created_at.desc())
    )
    return list(result.scalars().all())


async def approve_template(db, *, admin_uid: str, template_id: str) -> bool:
    """Moves a pending template to approved + is_public=True. Returns False if not found / not pending."""
    result = await db.execute(
        select(TemplateLibrary).where(
            TemplateLibrary.id == template_id,
            TemplateLibrary.status == "pending",
        )
    )
    tmpl = result.scalar_one_or_none()
    if tmpl is None:
        return False
    tmpl.status = "approved"
    tmpl.is_public = True
    tmpl.updated_at = datetime.now(timezone.utc)
    await db.commit()
    logger.info(
        "admin_action=approve_template admin_uid=%s template_id=%s",
        admin_uid,
        template_id,
    )
    return True


async def reject_template(db, *, admin_uid: str, template_id: str) -> bool:
    """Moves a pending template to rejected. Returns False if not found / not pending."""
    result = await db.execute(
        select(TemplateLibrary).where(
            TemplateLibrary.id == template_id,
            TemplateLibrary.status == "pending",
        )
    )
    tmpl = result.scalar_one_or_none()
    if tmpl is None:
        return False
    tmpl.status = "rejected"
    tmpl.is_public = False
    tmpl.updated_at = datetime.now(timezone.utc)
    await db.commit()
    logger.info(
        "admin_action=reject_template admin_uid=%s template_id=%s",
        admin_uid,
        template_id,
    )
    return True


async def update_pending(
    db,
    *,
    admin_uid: str,
    template_id: str,
    name: str,
    description: str | None,
    i18n: dict | None = None,
) -> bool:
    """Edits the name / description (+ translations) of a pending template before publication.

    ``i18n`` — a dictionary of translations of the form ``{"sr": {"name": …, "description": …}}`` (the same shape as the seeded templates, migration 0036). ``None`` — the translations are not touched: the operator may edit only the Russian text without filling in the languages. Empty values inside a language are discarded, so as not to save `""` instead of the absence of a translation — the render then honestly falls back.

    The keywords and classifications are NOT touched: these are search terms (the matching expands them with the dictionary), not text for reading.
    """
    result = await db.execute(
        select(TemplateLibrary).where(
            TemplateLibrary.id == template_id,
            TemplateLibrary.status == "pending",
        )
    )
    tmpl = result.scalar_one_or_none()
    if tmpl is None:
        return False
    tmpl.name = name
    tmpl.description = description
    if i18n is not None:
        cleaned = {
            lang: {k: v for k, v in (fields or {}).items() if v}
            for lang, fields in i18n.items()
        }
        tmpl.i18n = {lang: f for lang, f in cleaned.items() if f} or None
    tmpl.updated_at = datetime.now(timezone.utc)
    await db.commit()
    logger.info(
        "admin_action=update_pending admin_uid=%s template_id=%s",
        admin_uid,
        template_id,
    )
    return True
