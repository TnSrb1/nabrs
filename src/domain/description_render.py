"""Parsing a tender description into “document heading + text” blocks.

``src/parsers/jnportal/documents_fetch.py`` (a source adapter, not published), when extracting the text of the terms of reference, itself
inserts between the document texts a service separator ``### <file name>``
(our marker, not the source’s text) and glues the result into one string —
and that is what lands in ``tenders_normalized.description``. Both places where the description
is shown to the customer (the cabinet — ``_tender_description.html``, the public card —
``tender.html``) must render this marker as a document heading, not
print a literal of three hashes — hence a common predicate, not two copies.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_DOC_HEADING_PREFIX = "### "

# A service line of Excel extraction (``doc_extract._extract_xlsx/_extract_xls``
# write ``# <sheet name>`` before a sheet’s table). This is the markup of OUR extractor,
# not procurement text: we hide it only at render (in the DB and in the AI input it stays:
# the sheet name there separates the tables from each other and helps the model).
# Narrowed to the forms that actually occur in production descriptions (``# Sheet1``,
# ``# List1``, ``# Table 1``): the general ``^# \S.{0,80}$`` also hid legitimate document
# text of the form ``# 1 Predmet``. Sheet names in Cyrillic/Latin are NOT included here.
_EXTRACT_MARKER_RE = re.compile(r"^# (?:(?:Sheet|List)\w*(?: .{0,80})?|Table \d+)$")


@dataclass(frozen=True)
class DescriptionBlock:
    """One description block: ``heading`` is the document name (or ``None`` for
    text without our marker — for example, descriptions from other portals),
    ``text`` is the block body (may be empty if the document is empty)."""

    heading: str | None
    text: str


# Display cap for the description. jnportal stores the full document text (up to 300k,
# for stepwise AI analysis); pages must stay as light as when 50k was the storage cap.
DESCRIPTION_DISPLAY_MAX_CHARS = 50_000


def split_description_blocks(description: str | None) -> list[DescriptionBlock]:
    """Splits the raw description text by the marker ``### file name``.

    Lines NOT starting with the marker (including leading hyphens — this is the text
    of the documents themselves, not our markup) stay as is inside the block.
    """
    if not description:
        return []
    description = description[:DESCRIPTION_DISPLAY_MAX_CHARS]
    description = "\n".join(
        ln for ln in description.split("\n") if not _EXTRACT_MARKER_RE.match(ln)
    )
    blocks: list[DescriptionBlock] = []
    heading: str | None = None
    lines: list[str] = []
    for raw_line in description.split("\n"):
        if raw_line.startswith(_DOC_HEADING_PREFIX):
            blocks.append(
                DescriptionBlock(heading=heading, text="\n".join(lines).strip("\n"))
            )
            heading = raw_line[len(_DOC_HEADING_PREFIX) :].strip()
            lines = []
        else:
            lines.append(raw_line)
    blocks.append(DescriptionBlock(heading=heading, text="\n".join(lines).strip("\n")))
    return [b for b in blocks if b.heading or b.text]


def is_docs_only(blocks: list[DescriptionBlock]) -> bool:
    """A description made wholly of document excerpts (the first block — with a file name):
    the subject of procurement is not in the text, it is in the card title."""
    return bool(blocks) and blocks[0].heading is not None
