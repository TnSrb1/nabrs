"""The “Analitika” page in the cabinet — a report builder with export to Excel (R4).

Owner decision 10.10.2026 (docs/proposals/ANALYTICS_PRODUCT.md §4a, §6, §8):

* new analytics — ONLY in Business (and Enterprise); the analytics blocks already
  working on Pro (contracting authority, competitor, CSV) do not change;
* the trial period gets Business analytics for its 30 days.

Therefore the gate here is ITS OWN axis, not ``_is_pro_plus``:
``effective_tier ∈ {business, enterprise}`` OR ``status='trial'``. The freeze
(``is_frozen(access_until)``) — FIRST, as in ``customer_analytics_service._gate``:
a frozen trial does not get analytics.

Order in the transaction: ``set_tenant`` FIRST (tenants/profiles — tenant-scoped,
RLS), then the gate, then ``statement_timeout`` and reading the global
``tenders_normalized``/``tender_awards`` (they have no RLS). A timeout (57014) is not
a 500: rollback and a page with the hint “narrow the filter”.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any, Iterator, Mapping
from urllib.parse import urlencode

from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from src.analytics import report_builder as rb
from src.api import repo
from src.api.cabinet.analitika_xlsx import MonthValue, build_xlsx
from src.api.cabinet.customer_analytics_service import _TENANT_TIER_SQL
from src.api.cabinet.feed_service import _is_query_canceled
from src.api.cabinet.i18n import CABINET
from src.api.cabinet.library_sectors import cpv_label
from src.api.public.i18n import procedure_label
from src.domain.access import is_frozen
from src.domain.regions import OKRUG_CHOICES
from src.domain.tiers import effective_tier
from src.plans.catalog import get_plan, tier_to_plan_key

logger = logging.getLogger(__name__)

#: Time limit of the report (ms): 16k/13k rows are computed in fractions of a second; the limit
#: protects against a heavy combination of filters and growth of the volume.
ANALITIKA_STATEMENT_TIMEOUT_MS = 8000

_PROFILES_SQL = text("""
    SELECT id, name, classifications, regions,
           keywords_include, keywords_exclude, keyword_dict
    FROM profiles
    WHERE tenant_id = CAST(:tid AS uuid) AND deleted_at IS NULL
    ORDER BY created_at ASC, id ASC
""")

_REGION_NAMES = dict(OKRUG_CHOICES)

#: Order of the items in the dropdowns of the form (keys — from the builder's whitelists).
DIM_ORDER: tuple[str, ...] = (
    "customer",
    "winner",
    "month",
    "quarter",
    "cpv2",
    "cpv3",
    "region",
    "procedure",
    "source",
    "country",
)
MEASURE_ORDER: tuple[str, ...] = (
    "count",
    "sum",
    "avg",
    "median",
    "customers",
    "winners",
)


def analytics_gate(
    status: str | None, tier: str | None, access_until: datetime | None, now: datetime
) -> str | None:
    """Gate of new analytics → ``None`` (allowed) | ``frozen`` | ``gate_business``.

    The freeze FIRST. The trial is a separate branch: ``effective_tier`` gives the trial
    Pro rights, and Business analytics on the trial is the owner’s decision of 10.10.2026.

    The tier is decided by the catalog flag ``Plan.reports`` (#640, SSOT of the showcase and the gate):
    effective tier → plan (``tier_to_plan_key``) → ``.reports``. There is no second
    list of tiers here.
    """
    if is_frozen(access_until, now):
        return "frozen"
    if (status or "").strip().lower() == "trial":
        return None
    plan = get_plan(tier_to_plan_key(effective_tier(status, tier)))
    if plan is not None and plan.reports:
        return None
    return "gate_business"


async def _gate(db, tid: str) -> str | None:
    row = (await db.execute(_TENANT_TIER_SQL, {"tid": str(tid)})).first()
    if row is None:
        return "gate_business"
    return analytics_gate(
        getattr(row, "status", None),
        getattr(row, "tier", None),
        getattr(row, "access_until", None),
        datetime.now(timezone.utc),
    )


# --------------------------------------------------------------------------- #
#  View
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class ProfileOption:
    id: str
    name: str


@dataclass(frozen=True)
class RowView:
    g1: str
    g2: str
    n: int
    with_amount: int
    sum_fmt: str
    avg_fmt: str
    median_fmt: str
    customers: int
    winners: int


@dataclass(frozen=True)
class SortHead:
    """Column header: a sort link and its state."""

    href: str
    aria_sort: str  # ascending | descending | none
    arrow: str  # ▲ | ▼ | ""


@dataclass(frozen=True)
class Preset:
    key: str  # the caption key in the catalog
    href: str
    #: The current report parameters match the preset (highlights the button).
    active: bool = False


@dataclass(frozen=True)
class AnalitikaPageView:
    """``status``: ``success`` | ``gate_business`` | ``frozen``.

    With ``success``: ``ran`` — the report was executed (the form was submitted), ``timed_out``
    — hit the ``statement_timeout``.
    """

    status: str
    spec: rb.ReportSpec = field(default_factory=rb.ReportSpec)
    ran: bool = False
    timed_out: bool = False
    rows: list[RowView] = field(default_factory=list)
    total: RowView | None = None
    total_groups: int = 0
    first_date_fmt: str = ""
    last_date_fmt: str = ""
    profiles: list[ProfileOption] = field(default_factory=list)
    presets: list[Preset] = field(default_factory=list)
    export_href: str = ""
    heads: dict[str, SortHead] = field(default_factory=dict)
    dims: tuple[str, ...] = DIM_ORDER
    measures: tuple[str, ...] = MEASURE_ORDER
    regions: list[tuple[str, str]] = field(default_factory=lambda: OKRUG_CHOICES)


@dataclass(frozen=True)
class AnalitikaExportView:
    status: str  # success | gate_business | frozen | timeout
    filename: str = ""
    content: bytes = b""


def _fmt_eur(v: Any) -> str:
    if v is None:
        return ""
    return f"{int(round(float(v))):,}".replace(",", " ") + " €"


def _fmt_date(d: date | None) -> str:
    return d.strftime("%d.%m.%Y") if d else ""


def _t(lang: str) -> dict[str, str]:
    return CABINET.get(lang) or CABINET["sr"]


def dim_value_label(dim: str, value: str | None, lang: str) -> str:
    """Group key → caption in the interface language (NULL → “no data”)."""
    if value is None or value == "":
        return _t(lang)["an_unknown"]
    if dim == "region":
        return _REGION_NAMES.get(value, value)
    if dim == "procedure":
        return procedure_label(value, lang)
    if dim in ("cpv2", "cpv3"):
        name = cpv_label(value.ljust(8, "0"), lang)
        return f"{value} {name}" if name else value
    return value


def spec_query(spec: rb.ReportSpec) -> dict[str, str]:
    """Spec → query parameters (links of presets and export)."""
    q: dict[str, str] = {"subject": spec.subject, "g1": spec.group1, "m": spec.measure}
    if spec.group2:
        q["g2"] = spec.group2
    if spec.sort:
        q["sort"] = spec.sort
        q["dir"] = spec.sort_dir
    if spec.expiring_months:
        q["exp"] = str(spec.expiring_months)
    if spec.date_from:
        q["from"] = spec.date_from.isoformat()
    if spec.date_to:
        q["to"] = spec.date_to.isoformat()
    for key, val in (
        ("cpv", ",".join(spec.cpv)),
        ("region", spec.region),
        ("customer", spec.customer_q),
        ("winner", spec.winner_q),
        ("profile", spec.profile_id),
    ):
        if val:
            q[key] = val
    if spec.amount_min is not None:
        q["amin"] = str(spec.amount_min)
    if spec.amount_max is not None:
        q["amax"] = str(spec.amount_max)
    return q


def sort_heads(spec: rb.ReportSpec) -> dict[str, SortHead]:
    """Header links: a click → sort, a repeat → reverse order.

    The filters travel in the link in full (``spec_query``); ``timeout`` is not carried over.
    """
    cur_key, cur_dir = rb.effective_sort(spec)
    base = {k: v for k, v in spec_query(spec).items() if k not in ("sort", "dir")}
    heads: dict[str, SortHead] = {}
    for key in rb.SORT_COLUMNS:
        if not rb.sort_key_ok(key, spec.subject, bool(spec.group2)):
            continue
        if key == cur_key:
            nxt = "asc" if cur_dir == "desc" else "desc"
            aria = "ascending" if cur_dir == "asc" else "descending"
            arrow = "▲" if cur_dir == "asc" else "▼"
        else:
            nxt, aria, arrow = rb.default_sort_dir(key), "none", ""
        heads[key] = SortHead(
            href="/app/analitika?"
            + urlencode({**base, "sort": key, "dir": nxt})
            + "#an-result",
            aria_sort=aria,
            arrow=arrow,
        )
    return heads


#: Keys that do not take part in the comparison: sorting and the page do not change
#: the set of report rows, therefore the preset stays active after a click on a header.
_PRESET_SKIP_KEYS = ("sort", "dir", "page")


def _preset_active(params: dict[str, str], spec: rb.ReportSpec, ran: bool) -> bool:
    """The report was executed with exactly the preset’s parameters.

    The whole normalized report query (``spec_query``) is compared, not a set of keys: any changed
    filter of the form (period, CPV, region, customer, winner, amount) makes the report different and removes the highlight.
    """
    if not ran:
        return False
    current = {k: v for k, v in spec_query(spec).items() if k not in _PRESET_SKIP_KEYS}
    return current == params


def _presets(
    profiles: list[ProfileOption], spec: rb.ReportSpec | None = None, ran: bool = False
) -> list[Preset]:
    """Ready-made reports in one link; “my area” = the tenant’s first template."""
    mine = {"profile": profiles[0].id} if profiles else {}
    items = [
        (
            "an_preset_top_customers",
            {"subject": "tenders", "g1": "customer", "m": "count"},
        ),
        ("an_preset_winners", {"subject": "awards", "g1": "winner", "m": "sum"}),
        ("an_preset_monthly", {"subject": "tenders", "g1": "month", "m": "count"}),
        (
            "an_preset_expiring",
            {
                "subject": "awards",
                "g1": "customer",
                "g2": "cpv2",
                "m": "count",
                "exp": "12",
            },
        ),
    ]
    presets = []
    for k, q in items:
        params = {**q, **mine}
        presets.append(
            Preset(
                key=k,
                href="/app/analitika?" + urlencode(params) + "#an-result",
                active=spec is not None and _preset_active(params, spec, ran),
            )
        )
    return presets


async def _load_profiles(db, tid: str) -> list[Any]:
    return list((await db.execute(_PROFILES_SQL, {"tid": str(tid)})).fetchall())


def _apply_profile(spec: rb.ReportSpec, profiles: list[Any]) -> rb.ReportSpec:
    """Filter “as in my template”: the CPV prefixes and regions of the selected profile.

    The profile is searched ONLY among the profiles of its own tenant (already read
    under RLS) — a foreign id from the URL simply will not be found and will be discarded.
    """
    if not spec.profile_id:
        return spec
    for p in profiles:
        if str(p.id) == spec.profile_id:
            cpv, regions = rb.profile_filters(p.classifications, p.regions)
            kw = {
                "include": p.keywords_include or [],
                "exclude": list(p.keywords_exclude or []),
                "dict": bool(p.keyword_dict),
            }
            return replace(
                spec,
                profile_cpv=cpv,
                profile_regions=regions,
                profile_keywords=kw if rb.has_keywords(kw) else None,
            )
    return replace(spec, profile_id="")


def _row_view(r: Any, spec: rb.ReportSpec, lang: str) -> RowView:
    return RowView(
        g1=dim_value_label(spec.group1, r.g1_label, lang),
        g2=dim_value_label(spec.group2, r.g2_label, lang) if spec.group2 else "",
        n=r.n,
        with_amount=r.with_amount,
        sum_fmt=_fmt_eur(r.sum_eur),
        avg_fmt=_fmt_eur(r.avg_eur),
        median_fmt=_fmt_eur(r.median_eur),
        customers=r.distinct_customers,
        winners=r.distinct_winners,
    )


async def _run_guarded(db, tid: str, spec: rb.ReportSpec, *, limit: int, cov: bool):
    """The report under ``statement_timeout``; timeout → ``None`` (after the rollback)."""
    try:
        await db.execute(
            text("SELECT set_config('statement_timeout', :ms, true)"),
            {"ms": str(ANALITIKA_STATEMENT_TIMEOUT_MS)},
        )
        return await rb.run_report(db, spec, limit=limit, with_coverage=cov)
    except rb.KeywordScopeTooBroad:
        # Too many candidates for the text predicate: like a timeout —
        # the hint “narrow the filter” (period/CPV/district), not a truncated selection.
        logger.warning("analitika: keyword scope too broad")
        return None
    except DBAPIError as exc:
        if not _is_query_canceled(exc):
            raise
        logger.warning("analitika timeout: %s", type(exc.orig or exc).__name__)
        # The transaction after 57014 is aborted — rollback (it also resets the tenant GUC).
        await db.rollback()
        return None


async def get_analitika_page(
    db, *, tid: str, params: Mapping[str, Any], lang: str
) -> AnalitikaPageView:
    """Page: set_tenant → gate → profiles → (if the form was submitted) the report."""
    await repo.set_tenant(db, tid)
    gate = await _gate(db, tid)
    if gate:
        return AnalitikaPageView(status=gate)

    prows = await _load_profiles(db, tid)
    profiles = [ProfileOption(id=str(p.id), name=p.name or "—") for p in prows]
    spec = _apply_profile(rb.parse_spec(params), prows)
    ran = "subject" in params
    base = AnalitikaPageView(
        status="success",
        spec=spec,
        ran=ran,
        profiles=profiles,
        presets=_presets(profiles, spec, ran),
        export_href="/app/analitika.xlsx?" + urlencode(spec_query(spec)),
        heads=sort_heads(spec),
    )
    if not ran:
        return base
    if params.get("timeout") == "1":
        # Return from an export that hit the timeout: we show the hint right away,
        # we do not run the heavy request a second time (the flag is set only by the .xlsx route).
        return replace(base, timed_out=True)
    res = await _run_guarded(db, tid, spec, limit=rb.SCREEN_LIMIT, cov=False)
    if res is None:
        return replace(base, timed_out=True)
    t = res.totals
    total = RowView(
        g1="",
        g2="",
        n=t.n,
        with_amount=t.with_amount,
        sum_fmt=_fmt_eur(t.sum_eur),
        avg_fmt=_fmt_eur(t.avg_eur),
        median_fmt=_fmt_eur(t.median_eur),
        customers=t.distinct_customers,
        winners=t.distinct_winners,
    )
    return replace(
        base,
        rows=[_row_view(r, spec, lang) for r in res.rows],
        total=total,
        total_groups=res.total_groups,
        first_date_fmt=_fmt_date(t.first_date),
        last_date_fmt=_fmt_date(t.last_date),
    )


# --------------------------------------------------------------------------- #
#  Excel
# --------------------------------------------------------------------------- #


def _excel_key(dim: str, key: str | None, label: str) -> Any:
    """The group value for Excel: a month — as a date, the rest — as a caption."""
    if dim == "month" and key and len(key) == 7:
        try:
            return MonthValue(int(key[:4]), int(key[5:7]), 1)
        except ValueError:
            return label
    return label


def _num(v: Any) -> Any:
    if v is None:
        return None
    return float(v) if isinstance(v, Decimal) else v


def build_export(
    spec: rb.ReportSpec,
    res: rb.ReportResult,
    *,
    lang: str,
    generated_at: datetime,
    profile_name: str = "",
) -> bytes:
    """Report result → .xlsx (sheet Data + sheet Napomena)."""
    t = _t(lang)
    header = [t["an_dim_" + spec.group1]]
    if spec.group2:
        header.append(t["an_dim_" + spec.group2])
    header += [
        t["an_m_count"],
        t["an_with_amount"],
        t["an_m_sum"],
        t["an_m_avg"],
        t["an_m_median"],
        t["an_m_customers"],
    ]
    if spec.subject == "awards":
        header.append(t["an_m_winners"])

    def rows() -> Iterator[list[Any]]:
        for r in res.rows:
            g1 = dim_value_label(spec.group1, r.g1_label, lang)
            out: list[Any] = [_excel_key(spec.group1, r.g1_key, g1)]
            if spec.group2:
                g2 = dim_value_label(spec.group2, r.g2_label, lang)
                out.append(_excel_key(spec.group2, r.g2_key, g2))
            out += [
                r.n,
                r.with_amount,
                _num(r.sum_eur),
                _num(r.avg_eur),
                _num(r.median_eur),
                r.distinct_customers,
            ]
            if spec.subject == "awards":
                out.append(r.distinct_winners)
            yield out
        tt = res.totals
        total: list[Any] = [t["an_total"]] + ([""] if spec.group2 else [])
        total += [
            tt.n,
            tt.with_amount,
            _num(tt.sum_eur),
            _num(tt.avg_eur),
            _num(tt.median_eur),
            tt.distinct_customers,
        ]
        if spec.subject == "awards":
            total.append(tt.distinct_winners)
        yield total

    filters: list[str] = []
    if spec.date_from or spec.date_to:
        filters.append(
            f"{t['an_from']} {spec.date_from or '…'} — {t['an_to']} {spec.date_to or '…'}"
        )
    if spec.expiring_months:
        filters.append(t["an_x_expiring"].format(n=spec.expiring_months))
    if spec.cpv:
        filters.append("CPV " + ", ".join(p + "*" for p in spec.cpv))
    if spec.region:
        filters.append(
            f"{t['an_region']}: {_REGION_NAMES.get(spec.region, spec.region)}"
        )
    if spec.customer_q:
        filters.append(f"{t['an_customer_q']}: {spec.customer_q}")
    if spec.winner_q:
        filters.append(f"{t['an_winner_q']}: {spec.winner_q}")
    if spec.amount_min is not None:
        filters.append(f"{t['an_amin']}: {spec.amount_min}")
    if spec.amount_max is not None:
        filters.append(f"{t['an_amax']}: {spec.amount_max}")
    if spec.profile_id:
        cpv = ", ".join(p + "*" for p in spec.profile_cpv) or "—"
        regs = ", ".join(spec.profile_regions) or "—"
        kws = t["an_x_keywords_on"] if spec.profile_keywords else t["an_x_none"]
        filters.append(
            f"{t['an_profile']}: {profile_name} (CPV {cpv}; {regs}; "
            f"{t['an_x_keywords']}: {kws})"
        )

    cov = res.coverage
    tt = res.totals
    no_amount_share = round(100.0 * (tt.n - tt.with_amount) / tt.n, 1) if tt.n else 0.0
    subject_label = t["an_subject_" + spec.subject]
    notes: list[tuple[str, Any]] = [
        (t["an_x_dataset"], subject_label),
        (t["an_x_dataset_note"], t["an_x_note_" + spec.subject]),
        (t["an_x_filters"], "; ".join(filters) or t["an_x_none"]),
        (t["an_x_coverage_from"], cov.first_date),
        (t["an_x_coverage_to"], cov.last_date),
        (t["an_x_coverage_rows"], cov.n),
        (t["an_x_period_from"], tt.first_date),
        (t["an_x_period_to"], tt.last_date),
        (t["an_x_rows"], tt.n),
        (t["an_x_no_amount_share"], no_amount_share),
        (t["an_x_nulls"], t["an_x_nulls_note"]),
        (t["an_x_sources"], ", ".join(cov.sources) or "—"),
        (t["an_x_generated"], generated_at.replace(tzinfo=None)),
    ]
    if res.truncated:
        notes.append((t["an_x_truncated"], rb.EXPORT_LIMIT))
    return build_xlsx(header, rows(), notes, lang)


def export_filename(spec: rb.ReportSpec, generated_at: datetime) -> str:
    """ASCII file name only from the whitelist keys (user input does not get into the name)."""
    parts = ["analitika", spec.subject, spec.group1]
    if spec.group2:
        parts.append(spec.group2)
    parts.append(generated_at.strftime("%Y%m%d"))
    return "_".join(parts) + ".xlsx"


async def get_analitika_export(
    db, *, tid: str, params: Mapping[str, Any], lang: str
) -> AnalitikaExportView:
    """.xlsx export: the same gate and the same specification as the page."""
    await repo.set_tenant(db, tid)
    gate = await _gate(db, tid)
    if gate:
        return AnalitikaExportView(status=gate)
    prows = await _load_profiles(db, tid)
    spec = _apply_profile(rb.parse_spec(params), prows)
    profile_name = next(
        (p.name or "—" for p in prows if str(p.id) == spec.profile_id), ""
    )
    res = await _run_guarded(db, tid, spec, limit=rb.EXPORT_LIMIT, cov=True)
    if res is None:
        return AnalitikaExportView(status="timeout")
    now = datetime.now(timezone.utc)
    return AnalitikaExportView(
        status="success",
        filename=export_filename(spec, now),
        content=build_export(
            spec, res, lang=lang, generated_at=now, profile_name=profile_name
        ),
    )
