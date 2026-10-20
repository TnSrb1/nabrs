"""What the client gets from the source — the declaration of the adapter’s capabilities.

The “Sources” showcase (``GET /izvori``) and the row under a source in the profile form
promise the client concrete card fields. The promise is held NEXT TO the code
that fulfils it: the ``capabilities`` attribute on the adapter class, not a
list in the template. The template lists only the keys of this declaration.

The consistency guard — ``tests/parsers/test_source_capabilities.py``: any
registered adapter without a declaration is a red test; ``documents_text``
must match whether the adapter implements fetching documents.

Cross-source duplicate marks are NOT declared here: they are derived from the pairs of
:data:`src.dedup.linker` (what gets linked is decided by the dedup code, not by the text).

The module is a leaf: no network, DB or adapter imports (except the lazy
:func:`capabilities_by_key`, which loads the registry the same way the admin does).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from functools import lru_cache
from typing import Literal

logger = logging.getLogger(__name__)

#: ``always`` — the amount is in every card; ``if_stated`` — only if it was
#: stated by the customer; ``never`` — the source does not provide the amount.
AmountMode = Literal["always", "if_stated", "never"]

#: The card language — the key of the catalog ``cap_lang_<key>`` (:mod:`src.i18n_common`).
CardLanguage = Literal["sr", "en_original", "bhs"]

#: ``True`` — a separate description is in every card; ``"if_stated"`` — part of the
#: notices repeat the title in the description (TED: 7 of 10, measured 03.10.2026);
#: ``False`` — there is no description from the notice.
NoticeDescription = bool | Literal["if_stated"]

#: ``True`` — the deadline is in every card; ``"if_stated"`` — only if it
#: could be read from the notice (UKC: the deadline is in the text of the PDF “Poziv”; scans
#: have none, measured 06.10.2026); ``False`` — the source does not provide the deadline.
DeadlineMode = bool | Literal["if_stated"]


@dataclass(frozen=True)
class SourceCapabilities:
    """Tender card fields that the source really provides."""

    #: The bid submission deadline.
    deadline: DeadlineMode
    #: The estimated procurement value.
    amount: AmountMode
    #: The subject description from the notice itself.
    notice_description: NoticeDescription
    #: The text of the tender documentation (specification) is fetched — the AI analysis reads it,
    #: not only the notice.
    documents_text: bool
    #: CPV codes (used by the classifier matching).
    cpv: bool
    #: A direct link to the tender card on the source website.
    original_link: bool
    #: The decision on the selected winner (the “new win” alert).
    awards: bool
    #: The language in which the title and description come.
    card_language: CardLanguage
    #: The source’s website for people (``sources.base_url`` for some sources is the
    #: API address, not suitable for the client).
    public_url: str
    #: ``True`` — the source provides the closing hour; ``False`` — only the date (CFKU:
    #: ``closingDate`` without a time, the deadline is the end of the day in Belgrade, this is ours
    #: substitution). We show the client the date and a mark, without a placeholder hour.
    deadline_time: bool = True


@lru_cache(maxsize=1)
def _date_only_keys() -> frozenset[str]:
    """Sources whose deadline is a date only (``deadline_time=False``)."""
    return frozenset(
        key for key, caps in capabilities_by_key().items() if not caps.deadline_time
    )


def deadline_has_time(source_key: str | None) -> bool:
    """Whether to show the deadline hour for source ``source_key``.

    An unknown source (not in the registry, import failure) → ``True``: the behavior is
    as before, new sources do not lose the default hour.
    """
    return source_key not in _date_only_keys()


def capabilities_by_key() -> dict[str, SourceCapabilities]:
    """Declarations of all registered adapters: ``{source_key: caps}``.

    The registry is loaded lazily and best-effort (like ``admin.sources_admin``): an
    adapter import failure does not break the showcase — the sources are shown without the matrix.
    """
    try:
        from src.parsers.registry import load_all_adapters

        registry = load_all_adapters()
    except Exception as exc:  # noqa: BLE001 — the showcase lives without the registry
        logger.warning(
            "capabilities: adapter registry unavailable (%s)", type(exc).__name__
        )
        return {}
    out: dict[str, SourceCapabilities] = {}
    for key, cls in registry.items():
        caps = getattr(cls, "capabilities", None)
        if isinstance(caps, SourceCapabilities):
            out[key] = caps
    return out


#: A matrix row: ``(whether present, the cap_* label key in src.i18n_common)``.
CapabilityRow = tuple[bool, str]


def matrix_rows(caps: SourceCapabilities) -> list[CapabilityRow]:
    """The showcase matrix ``/izvori`` — the row order is the same for all sources.

    The amount “always” and “if stated” are DIFFERENT labels: “if stated” must not be
    shown as an unconditional promise (owner decision 02.10.2026).
    """
    amount_key = "cap_amount_if_stated" if caps.amount == "if_stated" else "cap_amount"
    return [
        (
            bool(caps.deadline),
            (
                "cap_deadline_if_stated"
                if caps.deadline == "if_stated"
                else "cap_deadline"
            ),
        ),
        (caps.amount != "never", amount_key),
        (
            bool(caps.notice_description),
            (
                "cap_notice_description_if_stated"
                if caps.notice_description == "if_stated"
                else "cap_notice_description"
            ),
        ),
        (
            caps.documents_text,
            "cap_documents_text" if caps.documents_text else "cap_documents_text_off",
        ),
        (caps.cpv, "cap_cpv"),
        (caps.original_link, "cap_original_link"),
        (caps.awards, "cap_awards"),
    ]


def short_keys(caps: SourceCapabilities) -> list[str]:
    """The short row under a source in the profile form — only what IS there."""
    keys: list[str] = []
    if caps.deadline == "if_stated":
        keys.append("cap_s_deadline_if_stated")
    elif caps.deadline:
        keys.append("cap_s_deadline")
    if caps.amount == "always":
        keys.append("cap_s_amount")
    elif caps.amount == "if_stated":
        keys.append("cap_s_amount_if_stated")
    if caps.notice_description == "if_stated":
        keys.append("cap_s_notice_description_if_stated")
    elif caps.notice_description:
        keys.append("cap_s_notice_description")
    if caps.documents_text:
        keys.append("cap_s_documents_text")
    if caps.cpv:
        keys.append("cap_s_cpv")
    if caps.awards:
        keys.append("cap_s_awards")
    return keys
