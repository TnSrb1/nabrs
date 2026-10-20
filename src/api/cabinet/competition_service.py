"""The “Konkurencija” block on the tender card: who won before (owner 13.10, step 4).

Two cuts over the last ``WINDOW_MONTHS`` months, top-``TOP_N`` in each:

1. with THIS customer (the key is the PIB; if the PIB is empty — the normalized name, as in
   ``customer_award_stats``: for tenders with awards ``customer_tax_id`` is empty);
2. in THIS CPV group — the first ``CPV_GROUP_DIGITS`` digits of the code (a CPV group, e.g.
   ``301`` = “Office equipment”). Two digits (the division) are too broad — “goods” as a whole;
   three — the same subject of procurement, but not a specific item.

ONLY the WINNERS from ``tender_awards``: the number of bids and the list of participants are not in
the open data, the block does not show or invent “participants”.

The unit of a win is the decision (``tender_id`` + the winner), not a table row: a decision over several lots has as many rows as there are lots, and ``award_amount_rsd``
in them repeats the sum of the WHOLE decision (on prod 1256 of 1433 such pairs — the same
sum). ``count(*)``/``sum`` over rows would inflate both the wins and the sums.

Joint bid (13.8% of rows): ``winner_pib`` — a comma-joined PIB list, in ``winner_name`` — a join of names, among which there are also commas, so splitting
it into separate contractors is unreliable. We show it as a group (“konzorcijum”), the group key —
the PIB string as is: the same consortium is summed up, and the consortium’s win is not attributed to each participant.

There is no plan gate (owner’s decision). The tables are public (RLS_MATRIX category U+0432), under
``tendersrb_app`` they are read through the default privileges from 0021 (checked by
``has_table_privilege``); ``set_tenant`` — FIRST, as in the whole cabinet.

The CPV query reads ``classifications`` (JSONB) of tenders with awards. Indexes for both
cuts — migration 0051 (``customer_key_idx`` and GIN on ``cpv_group3``); the measurement — in TESTING.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import text

from src.analytics.awards_analytics import _CUSTOMER_NAME_KEY, normalize_customer_name
from src.api import repo
from src.api.cabinet.customer_analytics_service import _fmt_date, _fmt_sum_rsd

WINDOW_MONTHS = 24
TOP_N = 5
CPV_GROUP_DIGITS = 3

_TENDER_SQL = text("""
    SELECT customer_name, customer_tax_id, classifications
    FROM tenders_normalized
    WHERE id = CAST(:tender_id AS uuid)
""")

# One decision = (tender_id, winner_pib): the lot rows collapse, the sum is the max
# (it repeats on all lots of the decision). Then — an aggregate by winner.
_RIVALS_SQL = """
    WITH decisions AS (
        SELECT a.tender_id,
               a.winner_pib,
               (array_agg(a.winner_name ORDER BY a.decision_date DESC, a.id DESC))[1]
                                                    AS winner_name,
               COALESCE(max(CAST(a.raw->>'winner_contracts_rsd' AS numeric)),
                        max(a.award_amount_rsd))    AS amount,
               max(a.decision_date)                 AS decided
        FROM tender_awards a
        {join}
        WHERE {predicate}
          AND a.tender_id <> CAST(:tender_id AS uuid)
          AND a.winner_pib IS NOT NULL AND a.winner_pib <> ''
          AND a.decision_date >= CAST(:cutoff AS date)
          AND a.decision_date <= CAST(:today AS date)
        GROUP BY a.tender_id, a.winner_pib
    )
    SELECT winner_pib,
           (array_agg(winner_name ORDER BY decided DESC))[1] AS winner_name,
           count(*)                                         AS wins,
           sum(amount)                                      AS sum_rsd,
           max(decided)                                     AS last_date
    FROM decisions
    GROUP BY winner_pib
    ORDER BY wins DESC, sum_rsd DESC NULLS LAST, last_date DESC, winner_pib
    LIMIT :limit
"""

_BY_PIB_SQL = text(_RIVALS_SQL.format(join="", predicate="a.customer_pib = :pib"))
_BY_NAME_SQL = text(
    _RIVALS_SQL.format(
        join="",
        predicate=_CUSTOMER_NAME_KEY.replace("customer_name", "a.customer_name")
        + " = :name",
    )
)
_BY_CPV_SQL = text(
    _RIVALS_SQL.format(
        join="JOIN tenders_normalized t ON t.id = a.tender_id",
        # cpv_group3 (migration 0051) cuts the codes to CPV_GROUP_DIGITS=3 and is covered by a GIN index;
        # changing the number of digits — only together with the function.
        predicate="cpv_group3(t.classifications) @> ARRAY[CAST(:group AS text)]",
    )
)


@dataclass(frozen=True)
class RivalView:
    """The winner in the cut: the values are already formatted for the template."""

    name: str
    pib: str  # "" for a consortium: the PIB join is not an identifier
    wins: int
    sum_fmt: str  # "" — the sum is not published for any decision
    last_fmt: str  # dd.mm.YYYY
    is_joint: bool


@dataclass(frozen=True)
class CompetitionView:
    """``customer``/``cpv`` — lists of top winners (empty = “no data”)."""

    customer_name: str = ""
    cpv_group: str = ""  # "" — the tender has no CPV code
    customer: list[RivalView] = field(default_factory=list)
    cpv: list[RivalView] = field(default_factory=list)
    months: int = WINDOW_MONTHS


def window_start(today: date, months: int = WINDOW_MONTHS) -> date:
    """Calendar “months ago”; the 29th–31st days are clamped to the end of the month."""
    total = today.year * 12 + today.month - 1 - months
    year, month = divmod(total, 12)
    month += 1
    nxt = date(year + (month == 12), month % 12 + 1, 1)
    last_day = (nxt - timedelta(days=1)).day
    return date(year, month, min(today.day, last_day))


def cpv_group(classifications: Any, digits: int = CPV_GROUP_DIGITS) -> str:
    """The CPV group of the tender: the first ``digits`` digits of the FIRST code in the array order."""
    if not isinstance(classifications, list):
        return ""
    for entry in classifications:
        if not isinstance(entry, dict) or entry.get("scheme") != "CPV":
            continue
        code = str(entry.get("code") or "").strip()
        if len(code) >= digits and code[:digits].isdigit():
            return code[:digits]
    return ""


def _rival(row: Any) -> RivalView:
    pib = str(row.winner_pib or "")
    joint = "," in pib
    total = row.sum_rsd if isinstance(row.sum_rsd, Decimal) else None
    return RivalView(
        name=str(row.winner_name or pib),
        pib="" if joint else pib,
        wins=int(row.wins),
        sum_fmt=_fmt_sum_rsd(total),
        last_fmt=_fmt_date(row.last_date),
        is_joint=joint,
    )


async def _rivals(db, sql, params: dict) -> list[RivalView]:
    rows = (await db.execute(sql, {**params, "limit": TOP_N})).fetchall()
    return [_rival(r) for r in rows]


async def get_competition(
    db, *, tid: str, tender_id: str, today: date | None = None
) -> CompetitionView:
    """Both cuts for the tender; no tender/customer/CPV → empty lists."""
    await repo.set_tenant(db, tid)
    row = (await db.execute(_TENDER_SQL, {"tender_id": tender_id})).first()
    if row is None:
        return CompetitionView()
    today = today or date.today()
    base = {
        "tender_id": tender_id,
        "cutoff": window_start(today),
        "today": today,
    }

    customer_name = str(row.customer_name or "").strip()
    customer: list[RivalView] = []
    if row.customer_tax_id:
        customer = await _rivals(
            db, _BY_PIB_SQL, {**base, "pib": str(row.customer_tax_id)}
        )
    elif customer_name:
        name = normalize_customer_name(customer_name)
        customer = await _rivals(db, _BY_NAME_SQL, {**base, "name": name})

    group = cpv_group(row.classifications)
    cpv: list[RivalView] = []
    if group:
        cpv = await _rivals(db, _BY_CPV_SQL, {**base, "group": group})
    return CompetitionView(
        customer_name=customer_name, cpv_group=group, customer=customer, cpv=cpv
    )
