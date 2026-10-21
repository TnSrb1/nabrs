"""Customer analytics page in the cabinet (A3, VG-06 lite).

Until now, customer analytics existed only AS A SECTION of the tender page
(``customer_analytics_service``): to look at a customer you first had to
find one of their tenders. Here the same analytics layer gets its own entry
point: the subscriber picks the customer himself (search by name) and sees the profile —
who wins, at what prices, how concentrated the market is.

What is reused AS IS (one gating axis, one predicate per meaning — the project rules), and not copied:

- ``_gate`` — freeze BEFORE the plan + Pro+ by the same predicate as the bot and the tender page. No new thresholds or plan lists here;
- ``customer_award_stats`` / ``customer_yearly_trend`` — the same aggregations;
- ``_view_from_stats`` — the same view assembly (amounts / period are already formatted).

Keying — strictly by the customer's NAME (``customer_pib`` is NOT passed to
``customer_award_stats``). This is not carelessness but a condition of consistency: the reference (``search_customers``) groups by name, and the profile
must count by the same predicate — otherwise the list promises “120 decisions”, and the profile
by tax ID would show 93 (22.9% of rows have an empty tax ID), and the figures would diverge in front of the user.
The tax ID (PIB) is shown for reference only.

Statuses: ``success`` | ``gate_pro`` | ``frozen``. Inside ``success`` the customer may be
not selected (``selected is None``) or selected but without data
(``selected_status='no_data'``) — this is NOT an error of the page but an empty state of the
section: a 404 here would lie (the customer exists, there are no decisions for it yet).

Tested on a mock session (ADR-013), like ``customer_analytics_service``.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from src.ai.usage import _TIER_LIMITS
from src.analytics.awards_analytics import (
    customer_award_stats,
    customer_yearly_trend,
    directory_sort,
    search_customers_page,
)
from src.api import repo
from src.api.cabinet.customer_analytics_service import (
    AnalyticsExportView,
    CustomerAnalyticsView,
    _fmt_date,
    _gate,
    _view_from_stats,
)
from src.bots.analytics_export import (
    EXPORT_TOP_N,
    build_customer_csv,
    customer_csv_filename,
)

# How many customers are on a reference page (and in the search results).
DIRECTORY_LIMIT = 20

# Input length caps: we cut HERE, not by route validation — a 422 JSON instead of
# the page is worse for a long paste into the search than a truncated query.
MAX_TERM_LEN = 120
# A name longer than the longest real customer simply will not be found →
# the no_data state (a clear hint), not an error.
MAX_NAME_LEN = 512


@dataclass(frozen=True)
class DirectoryItemView:
    """A customer selection row: name + volume of decisions (the date is already a string)."""

    name: str
    pib: str
    awards: int
    last_date_fmt: str  # "" if the decisions have no dates


@dataclass(frozen=True)
class NarucilacPageView:
    """State of the “Customer analytics” page.

    ``status`` — the gate of the whole page (``success``|``gate_pro``|``frozen``).
    On ``success``: ``candidates`` — the list to choose from, ``selected`` — the profile of the selected customer (None if not selected), ``selected_status`` —
    ``""`` (not selected) | ``success`` | ``no_data``.
    """

    status: str
    term: str = ""
    selected_name: str = ""
    selected_status: str = ""
    selected: CustomerAnalyticsView | None = None
    candidates: list[DirectoryItemView] = field(default_factory=list)
    sort: str = "decisions_desc"
    page: int = 1
    total_pages: int = 1
    total: int = 0


def _directory_view(rows) -> list[DirectoryItemView]:
    """Reference rows → view (the date of the last decision is formatted)."""
    return [
        DirectoryItemView(
            name=r.name,
            pib=r.pib or "",
            awards=r.awards,
            last_date_fmt=_fmt_date(r.last_date),
        )
        for r in rows
    ]


async def get_narucilac_page(
    db,
    *,
    tid: str,
    term: str | None = None,
    customer_name: str | None = None,
    sort: str | None = None,
    page: int = 1,
    months: int | None = None,
    tier_limits: dict[str, dict[str, int | None]] | None = None,
) -> NarucilacPageView:
    """Assembles the page: gate → reference → (optional) profile of the selected customer.

    The order is the same as for the other customer services: ``set_tenant`` as the FIRST
    action of the transaction (a single RLS invariant — the award tables are public, but
    ``_gate`` reads the tenant-scoped ``tenants``), then freeze / plan, and only then
    the read of the award data. Gate BEFORE the reference: a non-Pro user must not even get
    the list of customers.
    """
    limits = tier_limits if tier_limits is not None else _TIER_LIMITS

    # set_tenant FIRST in the transaction (the single RLS invariant).
    await repo.set_tenant(db, tid)

    gate_err = await _gate(db, tid=tid, limits=limits)
    if gate_err:
        return NarucilacPageView(status=gate_err)

    cleaned_term = (term or "").strip()[:MAX_TERM_LEN]
    selected_name = (customer_name or "").strip()[:MAX_NAME_LEN]

    sort_key = directory_sort(sort)
    page = max(1, page)
    directory = await search_customers_page(
        db,
        term=cleaned_term or None,
        limit=DIRECTORY_LIMIT,
        offset=(page - 1) * DIRECTORY_LIMIT,
        sort=sort_key,
    )
    if not directory.rows and page > 1:
        # A page past the end of the results (an old link, the search has narrowed): total in an empty
        # result is unknown (it comes as a window function over the rows), so we take
        # the first page for the total and redirect to the LAST one, not to the first.
        directory = await search_customers_page(
            db, term=cleaned_term or None, limit=DIRECTORY_LIMIT, sort=sort_key
        )
        page = max(1, -(-directory.total // DIRECTORY_LIMIT))
        if page > 1:
            directory = await search_customers_page(
                db,
                term=cleaned_term or None,
                limit=DIRECTORY_LIMIT,
                offset=(page - 1) * DIRECTORY_LIMIT,
                sort=sort_key,
            )
    candidates = _directory_view(directory.rows)
    total_pages = max(1, -(-directory.total // DIRECTORY_LIMIT))

    if not selected_name:
        return NarucilacPageView(
            status="success",
            term=cleaned_term,
            candidates=candidates,
            sort=sort_key,
            page=page,
            total=directory.total,
            total_pages=total_pages,
        )

    # Keyed by name — by the same predicate that computed the reference.
    stats = await customer_award_stats(db, customer_name=selected_name, months=months)
    if not stats.has_data:
        return NarucilacPageView(
            status="success",
            term=cleaned_term,
            selected_name=selected_name,
            selected_status="no_data",
            candidates=candidates,
            sort=sort_key,
            page=page,
            total=directory.total,
            total_pages=total_pages,
        )

    trend = await customer_yearly_trend(db, customer_name=selected_name)
    # The tax ID (PIB) — for reference, from the already loaded reference (no extra query).
    # Empty if the selected customer is not in the current results or the tax ID is unknown —
    # this does not affect the profile selection, whose key is the name.
    pib = next((c.pib for c in candidates if c.name == selected_name), "")
    return NarucilacPageView(
        status="success",
        term=cleaned_term,
        selected_name=selected_name,
        selected_status="success",
        selected=_view_from_stats(selected_name, pib, stats, trend),
        candidates=candidates,
        sort=sort_key,
        page=page,
        total=directory.total,
        total_pages=total_pages,
    )


async def get_narucilac_export(
    db,
    *,
    tid: str,
    customer_name: str,
    months: int | None = None,
    tier_limits: dict[str, dict[str, int | None]] | None = None,
    locale: str | None = None,
) -> AnalyticsExportView:
    """CSV export of the profile of the selected customer (C4).

    There is no second CSV contour: the format is assembled by the same ``build_customer_csv`` /
    ``customer_csv_filename`` as the bot export and the export from the tender page
    (UTF-8 BOM + ';' → Cyrillic opens in Excel). Here only the keying: the page selects the customer BY NAME, so the export must also
    count by the same predicate — otherwise the downloaded file would diverge from what the user
    saw on screen (see the module docstring about the tax ID).

    The order is the same as in ``get_narucilac_page``: ``set_tenant`` as the FIRST action of the
    transaction, then the gate (freeze / Pro+ — the shared ``_gate``, it has no threshold of its own),
    and only then the read of the award data. ``top_n=EXPORT_TOP_N`` — the file gets the full top (50),
    not the top five shown on the page: the export exists for working with the data further.

    The file name is keyed by the tax ID, and with an empty tax ID — by the name through ``_safe_ascii``,
    which strips non-ASCII: for a Cyrillic customer without a tax ID the file will be
    ``awards_customer_noname_all.csv``. This is left as is on purpose (the same builder as the bot); the content always starts with the row “Customer”.

    Statuses: ``success`` | ``gate_pro`` | ``frozen`` | ``no_customer``
    (customer not selected) | ``no_data``.
    """
    limits = tier_limits if tier_limits is not None else _TIER_LIMITS

    # set_tenant FIRST in the transaction (the single RLS invariant).
    await repo.set_tenant(db, tid)

    gate_err = await _gate(db, tid=tid, limits=limits)
    if gate_err:
        return AnalyticsExportView(status=gate_err)

    selected_name = (customer_name or "").strip()[:MAX_NAME_LEN]
    if not selected_name:
        return AnalyticsExportView(status="no_customer")

    stats = await customer_award_stats(
        db, customer_name=selected_name, months=months, top_n=EXPORT_TOP_N
    )
    if not stats.has_data:
        return AnalyticsExportView(status="no_data")

    return AnalyticsExportView(
        status="success",
        filename=customer_csv_filename(stats, customer_name=selected_name),
        content=build_customer_csv(stats, customer_name=selected_name, locale=locale),
    )
