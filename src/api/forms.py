"""Parsing and validation of the profile HTMX forms (Sprint-19).

Pure functions: the input is a flat ``Mapping`` (``request.form()``), the output is a normalized ``dict`` of the profile fields (DB_SCHEMA §2.6) + a list of errors. No DB / network — tested in isolation. The plan gate is applied separately (:mod:`src.api.tiers`) after the parsing.

The form field formats (HTMX, no SPA):
- ``keywords_include`` — a textarea, one AND group per line, terms comma-separated: ``video surveillance, installation`` → ``{"all_of": [...]}``. Between the lines — OR (DB_SCHEMA §2.6 / ARCHITECTURE §12.2).
- ``keywords_exclude`` — a comma-separated string of stop words.
- ``cls_include`` / ``cls_exclude`` — a textarea, one ``system:code`` per line (for example ``eu_cpv:32*``) → ``{"system_code","code"}``.
- ``regions`` / ``enabled_source_keys`` / ``procedure_types`` / ``languages`` / ``delivery_channels`` / ``contract_types`` — repeated fields (``getlist``) or a comma-separated string.
- ``customers_include`` / ``customers_exclude`` — repeated fields (checkbox chips); the string fallback is split by lines, NOT by comma: a comma is normal in a customer’s name (“…Electric Power Industry of Serbia, Belgrade”).
- numbers / time / booleans — converted; on a format error → a record in ``errors``.
"""

from __future__ import annotations

from typing import Any, Mapping

from src.domain.tiers import AUTO_ANALYSIS_LEVELS

#: The channels that really deliver are the only source of truth about
#: what to offer in the form (``routes_profiles.CHANNEL_CHOICES`` is built
#: from it). Adapters exist only for telegram and email: ``adapters/viber.py`` —
#: a stub (``NotImplementedError``), there is no web adapter at all, so viber / web
#: are NOT here — accepting them from the form would promise a delivery that will not happen.
#: Legacy values already in the DB are not broken by the form parsing: an unknown channel
#: is silently filtered out (the form does not render it either), and the profile is read as is.
AVAILABLE_CHANNELS = {"telegram", "email"}

# The sources (``enabled_source_keys``) — NOT a module constant: the list lives in the
# ``sources`` table (SRC-0a, R1). The form parsing only collects the keys;
# the selection of the valid ones is :func:`src.domain.sources.validate_source_keys` in
# ``profile_service`` (create / update), the default of a new profile —
# :func:`src.domain.sources.default_source_keys_for_new_profile`.
# ‼️ An empty ``enabled_source_keys`` does NOT mean “all sources”: stage A requires
# `:source_key = ANY(enabled_source_keys)`, so a profile without a single
# source matches NOTHING (in production two web profiles silently lived this way).
#: The contract type (``tenders_normalized.contract_type``): all adapters write exactly
#: this enum (measurement 01.10.2026); a foreign value from a forged form is discarded.
CONTRACT_TYPES = ("goods", "services", "works")
#: A safety guard against a forged form: this many customers in the list and such
#: a name length (the longest in production is ~200 characters) cover the live case.
MAX_CUSTOMERS = 50
_MAX_CUSTOMER_LEN = 300
_SCHEDULES = {"immediate", "digest_morning", "digest_evening", "digest_custom"}
_AI_LEVELS = {"none", "quick", "standard", "deep"}


def _getlist(form: Mapping[str, Any], key: str) -> list[str]:
    """A multi-value field (checkboxes / multi-select): ``getlist`` or a CSV string."""
    getlist = getattr(form, "getlist", None)
    if callable(getlist):
        values = getlist(key)
        if values:
            # Each value is also split by comma: a text field
            # (“repair, installation”) from a real FormData arrives as ONE string.
            # The values of checkboxes / multi-selects contain no commas.
            return [p.strip() for v in values for p in str(v).split(",") if p.strip()]
    raw = form.get(key)
    if not raw:
        return []
    return [p.strip() for p in str(raw).split(",") if p.strip()]


def _customers(form: Mapping[str, Any], key: str) -> list[str]:
    """The list of customers: names as in the source, without duplicates by case / spaces.

    The duplicates are compared with the same meaning as ``sql_customer_key`` (case and spaces do not distinguish a customer) — there is no point saving “EMS AD” and “ems  ad” twice. The name is stored with its original case, spaces collapsed.
    """
    getlist = getattr(form, "getlist", None)
    values = list(getlist(key)) if callable(getlist) else []
    if not values:
        raw = form.get(key)
        if isinstance(raw, (list, tuple)):
            values = list(raw)
        elif raw:
            values = str(raw).splitlines()
    out: list[str] = []
    seen: set[str] = set()
    for value in values:
        name = " ".join(str(value).split())[:_MAX_CUSTOMER_LEN]
        if name and name.lower() not in seen:
            seen.add(name.lower())
            out.append(name)
    return out[:MAX_CUSTOMERS]


def _csv(form: Mapping[str, Any], key: str) -> list[str]:
    """A single text field with comma-separated values → a list."""
    raw = form.get(key)
    if not raw:
        return []
    return [p.strip() for p in str(raw).split(",") if p.strip()]


def groups_to_textarea(groups: list[dict] | None) -> str:
    """Serializes the keywords_include groups into the form text (the inverse of _parse_keyword_groups).

    [{"all_of": ["video", "installation"]}, {"all_of": ["alarm"]}]
    → "video, installation\\nalarm"
    """
    if not groups:
        return ""
    lines = []
    for g in groups:
        terms = (g or {}).get("all_of") or []
        if terms:
            lines.append(", ".join(str(t) for t in terms))
    return "\n".join(lines)


def classifications_to_textarea(entries: list[dict] | None) -> str:
    """Serializes the classifications list into a textarea of the 'eu_cpv:CODE' format."""
    if not entries:
        return ""
    lines = []
    for e in entries or []:
        sc = (e or {}).get("system_code", "")
        code = (e or {}).get("code", "")
        if sc and code:
            lines.append(f"{sc}:{code}")
    return "\n".join(lines)


def _parse_keyword_groups(raw: str | None) -> list[dict]:
    """Text (a line = an AND group, terms comma-separated) → all_of groups."""
    groups: list[dict] = []
    for line in (raw or "").splitlines():
        terms = [t.strip() for t in line.split(",") if t.strip()]
        if terms:
            groups.append({"all_of": terms})
    return groups


def _parse_classifications(raw: str | None, errors: list[str], kind: str) -> list[dict]:
    """Lines ``system:code`` → ``[{"system_code","code"}]``."""
    out: list[dict] = []
    for line in (raw or "").splitlines():
        line = line.strip()
        if not line:
            continue
        if ":" not in line:
            # A catalog key instead of a phrase: the render resolves the string by the locale.
            # We do not substitute the value itself into the text — the error names the field, and
            # the concrete line is visible to the user in the input field itself.
            errors.append("err_cls_format")
            continue
        system, _, code = line.partition(":")
        system, code = system.strip(), code.strip()
        if not system or not code:
            errors.append("err_cls_empty")
            continue
        out.append({"system_code": system, "code": code})
    return out


def _to_decimal(value: str | None, error_key: str, errors: list[str]) -> float | None:
    """A number from the form; on error it puts a CATALOGUE KEY, not a ready phrase.

    ‼️ The form parser does not know the client’s language (it has no request). Therefore it returns keys (``err_num_min_amount``), and the render resolves them through `t` by the locale — the same contour that the profile_service errors already use (`profile_form.html`: ``t.get(e, e)``). Previously a Russian string was assembled here, and a Serb got it after the POST as is.
    """
    if value is None or str(value).strip() == "":
        return None
    try:
        return float(str(value).replace(" ", "").replace(",", "."))
    except ValueError:
        errors.append(error_key)
        return None


def _to_int(value: str | None, error_key: str, errors: list[str]) -> int | None:
    """An integer from the form; on error it puts a catalog KEY (see :func:`_to_decimal`)."""
    if value is None or str(value).strip() == "":
        return None
    try:
        return int(str(value).strip())
    except ValueError:
        errors.append(error_key)
        return None


def _to_bool(value: Any) -> bool:
    return str(value).strip().lower() in {"1", "true", "on", "yes"}


def _norm_time(value: str | None) -> str | None:
    """Accepts ``HH:MM`` / ``HH:MM:SS``; empty → None. Light normalization."""
    v = (value or "").strip()
    return v or None


def parse_profile_form(form: Mapping[str, Any]) -> tuple[dict, list[str]]:
    """Parses the profile form. Returns (payload, errors).

    ``payload`` contains all fields of §12.1 in a form suitable for the ORM model ``Profile`` (JSONB as dict, arrays as list, time as a string ``HH:MM``). The plan gate is NOT applied here (see :func:`src.api.tiers.gate_profile`).
    """
    errors: list[str] = []

    include_groups = _parse_keyword_groups(form.get("keywords_include"))
    classifications = {
        "include": _parse_classifications(form.get("cls_include"), errors, "include"),
        "exclude": _parse_classifications(form.get("cls_exclude"), errors, "exclude"),
    }

    channels = [
        c for c in _getlist(form, "delivery_channels") if c in AVAILABLE_CHANNELS
    ]

    schedule = (form.get("delivery_schedule") or "immediate").strip()
    if schedule not in _SCHEDULES:
        # The value comes from a <select> — normally unreachable; a key instead of a phrase.
        errors.append("err_bad_schedule")
        schedule = "immediate"

    # The automatic AI analysis of the stream: EVERY delivered tender is analyzed,
    # that is, the subscription spends money without a click. Therefore the value is accepted
    # STRICTLY from the allow-list (``AUTO_ANALYSIS_LEVELS`` = none / standard), and everything
    # else — including ``quick`` / ``deep`` sent by hand — is treated as
    # “do not analyze”: “a paid call is switched on by forging the form” is a hole, not
    # flexibility. We do not show an error to the user: such values do not exist in the form,
    # only a non-form client could send them.
    #
    # The form is NOT the last line: the plan / status / limit is checked by
    # ``src.ai.usage.auto_limit_decision`` in the worker before the model call.
    ai_level = (form.get("ai_analysis_level") or "none").strip().lower()
    if ai_level not in AUTO_ANALYSIS_LEVELS:
        ai_level = "none"

    # The “All tenders” mode (0043): the whole portal stream without selection by topic.
    # ‼️ AI is switched OFF in this mode FORCIBLY, not by a hint in the markup:
    # the stream is ~170 notices a day, analyzing each one with the model is direct money,
    # and “a new paid call = gate + limit + metering” (CLAUDE.md). To silently give
    # such a profile the deep level would mean opening unlimited spend to whoever
    # simply pressed “Show all”. A client who needs AI sets a topic.
    match_all = _to_bool(form.get("match_all"))
    if match_all:
        ai_level = "none"

    # The name is NOT mandatory: empty → the backend substitutes the auto-generated “Profile N”
    # (:func:`profile_service.create_profile`). Nothing in the profile form
    # we do not make it mandatory (the subscribers' complaint: "a template must not have
    # anything mandatory").
    name = (form.get("name") or "").strip()

    # A customer in both lists: “exclude” is stronger, and in the matching too — in the form
    # we leave it only there, so that what is saved matches what will fire.
    customers_exclude = _customers(form, "customers_exclude")
    excluded = {c.lower() for c in customers_exclude}
    customers_include = [
        c for c in _customers(form, "customers_include") if c.lower() not in excluded
    ]

    focus_raw = form.get("ai_focus_questions")
    focus_questions = [q.strip() for q in (focus_raw or "").splitlines() if q.strip()]

    payload: dict = {
        "name": name,
        "target_language": (form.get("target_language") or "ru").strip(),
        "is_active": _to_bool(form.get("is_active", "on")),
        "debug_match": _to_bool(form.get("debug_match")),
        "keyword_dict": _to_bool(form.get("keyword_dict")),
        "match_all": match_all,
        "enabled_source_keys": _getlist(form, "enabled_source_keys"),
        "keywords_include": include_groups,
        "keywords_exclude": _getlist(form, "keywords_exclude"),
        "classifications": classifications,
        "regions": _getlist(form, "regions"),
        "min_amount_eur": _to_decimal(
            form.get("min_amount_eur"), "err_num_min_amount", errors
        ),
        "max_amount_eur": _to_decimal(
            form.get("max_amount_eur"), "err_num_max_amount", errors
        ),
        "include_no_amount": _to_bool(form.get("include_no_amount")),
        "procedure_types": _getlist(form, "procedure_types"),
        "contract_types": [
            c for c in CONTRACT_TYPES if c in _getlist(form, "contract_types")
        ],
        "customers_include": customers_include,
        "customers_exclude": customers_exclude,
        "languages": _getlist(form, "languages"),
        "min_days_until_deadline": _to_int(
            form.get("min_days_until_deadline"), "err_int_min_days", errors
        ),
        "ai_analysis_level": ai_level,
        "ai_focus_questions": focus_questions,
        "ai_extra_context": (form.get("ai_extra_context") or "").strip() or None,
        "delivery_channels": channels,
        "delivery_schedule": schedule,
        "digest_cron": (form.get("digest_cron") or "").strip() or None,
        "quiet_hours_start": _norm_time(form.get("quiet_hours_start")),
        "quiet_hours_end": _norm_time(form.get("quiet_hours_end")),
    }
    return payload, errors
