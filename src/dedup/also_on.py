"""The mark “the same tender is also on other sources” — ONE predicate for all renderings.

A tender linked to a cross-source cluster (:mod:`src.dedup.linker`, 0047) is seen by
the client in several places: the public card, the feed and the teaser of the cabinet dashboard,
the tender card in the cabinet, the card and the digest of Telegram, the email and the digest by email.
The mark must look the same everywhere, therefore both the number and the choice of the row live
here, and not at each rendering point:

* N — the number of OTHER sources of the cluster (``COUNT(DISTINCT source_key)`` without its own),
  not of rows: a second jnportal row in a TED cluster is not “one more source”;
* ``canonical_confidence = 'probable'`` → ``also_on_probable``, otherwise ``also_on``
  (the same fork as ``tender.html``); N = 0 → there is no mark.

The rows — in the catalogs of the contour: ``also_on``/``also_on_probable`` in
:mod:`src.i18n_common` (the bot + the cabinet) and :mod:`src.i18n_email` (the emails), with ``{n}``.
"""

from __future__ import annotations

from collections.abc import Callable

__all__ = ["ALSO_ON_KEY", "ALSO_ON_PROBABLE_KEY", "also_on_count_sql", "also_on_text"]

ALSO_ON_KEY = "also_on"
ALSO_ON_PROBABLE_KEY = "also_on_probable"


def also_on_count_sql(alias: str) -> str:
    """The SQL expression "how many OTHER platforms are in the cluster of the row ``alias``".

    It is embedded into the SELECT of the same sample that renders the row (without N+1).
    The ``CASE`` cuts the subquery for the non-clustered rows (almost all), for the clustered ones it goes
    by the index ``tenders_normalized(canonical_tender_id)``.
    ``alias`` — only a literal from the code (the table name in FROM), not input.
    """
    return (
        f"CASE WHEN {alias}.canonical_tender_id IS NULL THEN 0 ELSE ("
        "SELECT COUNT(DISTINCT also_on_tn.source_key) "
        "FROM tenders_normalized also_on_tn "
        f"WHERE also_on_tn.canonical_tender_id = {alias}.canonical_tender_id "
        f"AND also_on_tn.source_key <> {alias}.source_key) END"
    )


def also_on_text(
    translate: Callable[[str | None, str], str],
    locale: str | None,
    confidence: str | None,
    count: int | None,
) -> str | None:
    """The mark row from the catalog ``translate`` (``common_t``/``email_t``) or None."""
    if not count or count <= 0:
        return None
    key = ALSO_ON_PROBABLE_KEY if confidence == "probable" else ALSO_ON_KEY
    return translate(locale, key).format(n=count)
