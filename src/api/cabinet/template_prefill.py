"""The profile prefill from a library template / a vertical package — ONE point.

Two places live off the same payload:

1. the new-profile form (``/profiles/new?from_template=…``) — what the client sees in the fields;
2. the figure “≈ N tenders in 30 days” on the showcase card ``/library``.

If the showcase counted over its own set of fields, the figure on the card would diverge from the figure of the form preview on the same day — and it could not be trusted. Therefore the card and the form take the payload FROM HERE, and one :func:`src.api.cabinet.preview_service.preview_count` counts it.

What the prefill ADDS to the template data (and why):

- ``keyword_dict=True`` — the template keywords are Russian, the notices are Serbian; the bridge between them is the forms dictionary. Without it the template “Software and licenses” gave 0 matches instead of 83 (production measurement 04.08). The tick stays visible and removable by hand — only the DEFAULT value for a new profile changes; existing profiles in the DB are not touched;
- ``enabled_source_keys`` — the sources ticked by default. The module is pure (no DB), so the list comes from the caller as the ``source_keys`` argument — this is :func:`src.domain.sources.default_source_keys_for_new_profile` (the ``sources`` registry, SRC-0a). An empty list = “not a single source” = zero matches;
- CPV — as prefix templates (:func:`src.domain.classifications.cpv_prefix`): the library has codes at the section level, the notices have the codes of descendants.

The template texts in the DB are NOT rewritten: the prefill is a derivative AT READ TIME.
"""

from __future__ import annotations

from typing import Any, Sequence

from src.api.cabinet import vertical_packages
from src.domain.classifications import as_prefix_filters


def _copy_groups(groups: Any) -> list[dict]:
    """A deep copy of the OR-groups ``[{"all_of": [...]}]`` (the catalog is immutable)."""
    out: list[dict] = []
    for group in groups or []:
        if isinstance(group, dict):
            out.append({"all_of": list(group.get("all_of") or [])})
    return out


def base_payload(*, source_keys: Sequence[str] = (), **overrides: Any) -> dict:
    """The profile payload with the defaults for a NEW profile.

    The keys and their meaning are the contract of :func:`src.api.forms.parse_profile_form`; the same dict is read by :func:`preview_service.preview_count`, so the preview figure and the form fields are counted from the same values.
    """
    payload: dict = {
        "name": "",
        # ‼️ An explicit default: without the key `_apply_payload` (profile_service.py) skips it,
        # and the column `profiles.is_active` without a server_default gives
        # NULL on insert — the matching’s `WHERE p.is_active = true` (matching.py)
        # then silently does not find the profile (NULL = true → NULL, not true). Right now the
        # only call of create_profile goes through parse_profile_form,
        # which always puts a bool, so the bug does not show — a hole for the case of a
        # future direct call of create_profile with this payload.
        "is_active": True,
        "keyword_dict": True,
        "enabled_source_keys": list(source_keys),
        "keywords_include": [],
        "keywords_exclude": [],
        "classifications": {"include": [], "exclude": []},
        "regions": [],
        "min_amount_eur": None,
        "max_amount_eur": None,
        "min_days_until_deadline": None,
        "include_no_amount": False,
        # The “All tenders” mode — only by explicit choice (0043); by default
        # the profile selects by topic.
        "match_all": False,
    }
    payload.update(overrides)
    return payload


def from_template(tpl: Any, *, source_keys: Sequence[str] = ()) -> dict:
    """A library template (the ORM row of ``template_library``) → the profile payload."""
    cls = tpl.classifications if isinstance(tpl.classifications, dict) else {}
    return base_payload(
        source_keys=source_keys,
        name=tpl.name or "",
        keywords_include=_copy_groups(tpl.keywords_include),
        classifications={
            "include": as_prefix_filters(cls.get("include")),
            "exclude": as_prefix_filters(cls.get("exclude")),
        },
    )


def from_package(
    pkg: vertical_packages.VerticalPackage,
    lang: str = "ru",
    *,
    source_keys: Sequence[str] = (),
) -> dict:
    """A vertical package (data-as-code) → the profile payload."""
    prefill = vertical_packages.to_profile_prefill(pkg, lang)
    cls = prefill.get("classifications") or {}
    return base_payload(
        source_keys=source_keys,
        name=prefill.get("name") or "",
        keywords_include=_copy_groups(prefill.get("keywords_include")),
        keywords_exclude=list(prefill.get("keywords_exclude") or []),
        classifications={
            "include": as_prefix_filters(cls.get("include")),
            "exclude": as_prefix_filters(cls.get("exclude")),
        },
    )


def unfiltered_payload(*, source_keys: Sequence[str] = ()) -> dict:
    """The “no filter” payload — a profile that shows ALL tenders.

    There are no keywords and CPV, and the mode is declared EXPLICITLY by the flag ``match_all`` (0043): before it, “show everything” followed from the emptiness of the profile and was indistinguishable from “the client has not configured the filters” — with the same result in the form of a mirror of the portal (≈2000 notices over 30 days in production). Now the whole stream is received only by whoever chose it; a profile without a topic and without the flag matches nothing.

    The AI analysis in this mode is forcibly switched off when the form is parsed (:func:`src.api.forms.parse_profile_form`) — ~170 notices a day through the model is direct money.
    """
    return base_payload(source_keys=source_keys, match_all=True)
