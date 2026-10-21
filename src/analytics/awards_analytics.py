"""VG-06 — analytics of the contracting authority and competitors over ``tender_awards``.

A pure query layer (read-only): takes ``session`` (AsyncSession),
returns dataclasses. No network, no AI, no writes — cheap SELECT aggregations.
The second differentiator of the product after the AI verdict: "who wins and at what price
with this customer“ / ”how strong this competitor is".

Keying:
- The contracting authority (customer) — by ``tender_awards.customer_pib`` (the customer's PIB), and when
  the PIB is empty — by the NORMALIZED name (``_CUSTOMER_NAME_KEY``). On live
  data the name branch is the one that works: for tenders with award rows
  ``customer_tax_id`` is empty in 100% of cases (0 out of 1454), whereas ``customer_name``
  is filled in 100%.
- The competitor (the winner) — by ``tender_awards.winner_pib``.

Window: ``months=None`` → all time; ``months=N`` → ``decision_date`` for the
last N months. Award data on eNabavke is skewed to the past (decisions on
old tenders) → the default over the whole period is more informative.

The sample period is returned TOGETHER with the aggregate (``first_date``/``last_date``/
``without_date``), and is not implied: an aggregate “for all time” without boundaries lies
exactly like the famous “88% of tenders without a deadline” (a backfill artifact — 95% among
fresh ones). Showing “5 decisions” without “for 2019–2026, another 22 rows without a date”
means repeating the same error. The time axis on top of this is ``customer_yearly_trend``.

An asyncpg pitfall (see usage.py): an optional named parameter in text() SQL
that is sometimes there and sometimes not breaks the type binding. Therefore two SQL variants —
``*_ALL`` (without the date filter) and ``*_WINDOW`` (with ``decision_date >= :cutoff``) —
are chosen by the ``months is None`` branch, and not by one query with an optional parameter.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from sqlalchemy import text

from src.domain.text_norm import fold, fold_sql

# --------------------------------------------------------------------------- #
#  The customer key by name — ONE formula for the whole module
# --------------------------------------------------------------------------- #

# Names come from the portal in different forms: the same organization appears with a double
# space inside, with a trailing space, in another case. Exact match on
# ``customer_name`` split such strings into DIFFERENT customers and silently undercounted:
# on the live production “ZAVOD ZA JAVNO ZDRAVLJE SABAC” — 21 rows + 9 rows of the variant
# with a double space, i.e. the profile showed 21 decisions out of 30 (−30%). The defect
# is invisible: the figure looks plausible, and there is nothing to check it against.
#
# ‼️ The formula is ONE here (SQL) and in ``normalize_customer_name`` (Python, the side
# of the bind parameter) — both sides of the comparison must normalize the same way, otherwise
# the predicate finds NOTHING. The equivalence of the two implementations is pinned by a test
# (``tests/analytics/test_awards_incumbent.py``) and verified on all 1454 live
# rows. There is no index on ``customer_name`` (see pg_indexes) — the exact match also went
# by seq-scan, so the normalization does not change the plan on a table of this size.
_CUSTOMER_NAME_KEY = r"lower(btrim(regexp_replace(customer_name, '\s+', ' ', 'g')))"


# The SEARCH key: the same key, folded to ASCII Latin without diacritics
# (``text_norm.fold`` in SQL). The names in the DB are in Cyrillic, while the sr interface is
# Latin: without folding “beograd” did not find “BEOGRAD” (in Cyrillic in the DB). Only for the HAVING search;
# the GROUPING and profile key stays ``_CUSTOMER_NAME_KEY``.
_CUSTOMER_NAME_FOLDED = fold_sql(_CUSTOMER_NAME_KEY, lowered=True)


def normalize_customer_name(name: str) -> str:
    """The customer name → matching key (a mirror of ``_CUSTOMER_NAME_KEY``).

    The order of steps matches the SQL: collapse whitespace runs → trim the edges →
    lowercase. A divergence from the SQL formula = the predicate stops finding rows, therefore
    these two implementations may be changed only together.
    """
    return re.sub(r"\s+", " ", name).strip().lower()


# --------------------------------------------------------------------------- #
#  SQL — the customer profile (by customer_pib)
# --------------------------------------------------------------------------- #


# The amount of a DECISION (tender_id, winner_pib). For the winner of one lot — the contract of the lot; for a
# winner of several lots the lot amount is NULL (a contract has no lot key), but in
# each of its rows lies ``raw.winner_contracts_rsd`` — the sum of all its contracts on the
# tender. Old rows without the field — the former ``max(award_amount_rsd)``.
_DECISION_RSD = "COALESCE(max(CAST(raw->>'winner_contracts_rsd' AS numeric)), max(award_amount_rsd))"
# EUR — the same rule as RSD: the sum of ALL the winner’s contracts (raw.winner_contracts_eur,
# the rate of the batch). Old rows without the field and non-RSD winners — the former max(award_amount_eur).
_DECISION_EUR = "COALESCE(max(CAST(raw->>'winner_contracts_eur' AS numeric)), max(award_amount_eur))"


# The customer’s decisions are the UNIT of all aggregates of the summary/trend/reference (issue #687,
# #696). A ``tender_awards`` row = a LOT, and the amount in ``award_amount_rsd`` is repeated on
# each lot of the decision: a row count would give “3 decisions, 900 RSD” for one tender with three
# lots, whereas there is one decision, for 300 RSD. Therefore first we collapse the lots
# into the decision: the amount is ``max``, not ``sum`` (see ``_CUSTOMER_RECENT_AWARDS_BY_NAME``).
# The decision key — (tender_id, winner_pib), as for the top winners and
# ``competition_service._RIVALS_SQL``. For winners without a PIB the key is supplemented by the
# name: otherwise different PIB-less firms of one tender would merge into one decision.
#
# A template, not copies of the SQL: the predicate is a module-level literal (not user input).
def decisions_cte(where: str, extra_cols: str = "") -> str:
    """The CTE ``decisions``: one row per decision (the ``where`` predicate).

    The common counting unit of the analytics AND of the public storefront (``stats_page``): the key and
    the amount are here, there are no copies. ``extra_cols`` — extra aggregates, comma-separated at the end
    (``"max(x) AS x"``); empty by default — the analytics behavior does not change.
    """
    extra = f",\n            {extra_cols}" if extra_cols else ""
    return f"""WITH decisions AS (
        SELECT
            tender_id,
            winner_pib,
            {_DECISION_RSD}           AS amount_rsd,
            {_DECISION_EUR}           AS amount_eur,
            max(decision_date)              AS decision_date{extra}
        FROM tender_awards
        WHERE {where}
        GROUP BY tender_id, winner_pib, CASE WHEN winner_pib IS NULL THEN winner_name END
    )"""


# The common list of summary aggregates — ONE source of truth for all 4 variants of the predicate
# (pib/name × all/window). Earlier the columns were copied into each text() and
# would diverge at the first edit: add a median in one — forget it in three.
# Computed over ``decisions`` (see above), not over the lot rows.
#
# avg/median are computed over the decisions with a known amount (NULL amounts are ignored by the aggregates and
# percentile_cont) → their denominator is ``with_amount``, and NOT
# ``total_awards``: for 22.9% of rows the customer is empty, the amounts appear only after
# of the contract registration. The median is more honest than the mean on prize-like distributions —
# one framework contract of hundreds of millions skews the avg, the median does not notice it.
_SUMMARY_COLS = """
        count(*)                                                    AS total_awards,
        count(DISTINCT winner_pib) FILTER (WHERE winner_pib IS NOT NULL) AS distinct_winners,
        count(amount_rsd)                                           AS with_amount,
        COALESCE(sum(amount_rsd), 0)                                AS total_rsd,
        COALESCE(sum(amount_eur), 0)                                AS total_eur,
        avg(amount_rsd)                                             AS avg_rsd,
        percentile_cont(0.5) WITHIN GROUP (
            ORDER BY amount_rsd::double precision)                  AS median_rsd,
        min(decision_date)                                          AS first_date,
        max(decision_date)                                          AS last_date,
        count(*) FILTER (WHERE decision_date IS NULL)               AS without_date
"""


def _summary_sql(where: str):
    """The customer summary with the ``where`` predicate (a module-level literal, not user input)."""
    return text(f"{decisions_cte(where)}\n    SELECT{_SUMMARY_COLS}    FROM decisions")


_CUSTOMER_SUMMARY_ALL = _summary_sql("customer_pib = :pib")

_CUSTOMER_SUMMARY_WINDOW = _summary_sql(
    "customer_pib = :pib\n      AND decision_date >= :cutoff"
)


# Top winners of the customer. The UNIT OF A WIN is the DECISION (tender_id, winner_pib), and not
# a lot row (issue #687): a decision over several lots has as many rows as there are
# lots, while ``award_amount_rsd`` is repeated on each of them. Therefore first we collapse the lots
# into the decision (the amount is max, not sum: see ``_CUSTOMER_RECENT_AWARDS_BY_NAME``), and
# only then count the wins and the amount per winner. The same semantics as
# ``competition_service._RIVALS_SQL`` (the “Konkurencija” block) — two blocks about one
# customer must not diverge in the number of wins.
#
# A template, not four copies of the SQL: the predicate is a module-level literal (not user input).
def _winners_sql(where: str):
    """Top winners of the contracting authority with the ``where`` predicate (see the block above)."""
    return text(f"""
        WITH decisions AS (
            SELECT
                tender_id,
                winner_pib,
                (array_agg(winner_name ORDER BY decision_date DESC NULLS LAST, id DESC))[1]
                                                    AS winner_name,
                {_DECISION_RSD}               AS amount_rsd,
                max(decision_date)                  AS decided
            FROM tender_awards
            WHERE {where} AND winner_pib IS NOT NULL
            GROUP BY tender_id, winner_pib
        )
        SELECT
            (array_agg(winner_name ORDER BY decided DESC NULLS LAST))[1] AS winner_name,
            winner_pib,
            count(*)                            AS wins,
            COALESCE(sum(amount_rsd), 0)        AS sum_rsd
        FROM decisions
        GROUP BY winner_pib
        ORDER BY wins DESC, sum_rsd DESC, winner_pib
        LIMIT :limit
    """)


_CUSTOMER_TOP_WINNERS_ALL = _winners_sql("customer_pib = :pib")

_CUSTOMER_TOP_WINNERS_WINDOW = _winners_sql(
    "customer_pib = :pib\n      AND decision_date >= :cutoff"
)

# Fallback by the customer name (customer_name) when the PIB is empty. In our database ~93% of
# tenders have customer_name, but no customer_tax_id (PIB) → keying only by the PIB would cover
# ~7% of tenders (and on the subset of rows WITH an award — 0%: for all 1454
# the tender’s PIB is empty, see A4). Matching is by the NORMALIZED name
# (``_CUSTOMER_NAME_KEY``), not the verbatim one: spelling variants of one organization
# otherwise split into different “customers”.
_CUSTOMER_SUMMARY_BY_NAME_ALL = _summary_sql(f"{_CUSTOMER_NAME_KEY} = :name")

_CUSTOMER_SUMMARY_BY_NAME_WINDOW = _summary_sql(
    f"{_CUSTOMER_NAME_KEY} = :name\n      AND decision_date >= :cutoff"
)

_CUSTOMER_TOP_WINNERS_BY_NAME_ALL = _winners_sql(f"{_CUSTOMER_NAME_KEY} = :name")

_CUSTOMER_TOP_WINNERS_BY_NAME_WINDOW = _winners_sql(
    f"{_CUSTOMER_NAME_KEY} = :name\n      AND decision_date >= :cutoff"
)


# Dynamics by year: how many decisions, how many different winners and the amount for each year.
# The trend is by its nature “for all time” (the months window is not applicable) → without
# a WINDOW variant. The year is from decision_date; rows without a date do not take part.
# The year — by the decision (``decisions.decision_date``), the number of decisions and the amount — by the decision.
def _trend_sql(where: str):
    return text(f"""{decisions_cte(where)}
    SELECT
        EXTRACT(YEAR FROM decision_date)::int                            AS yr,
        count(*)                                                         AS awards,
        count(DISTINCT winner_pib) FILTER (WHERE winner_pib IS NOT NULL) AS distinct_winners,
        COALESCE(sum(amount_rsd), 0)                                     AS total_rsd,
        COALESCE(sum(amount_eur), 0)                                     AS total_eur
    FROM decisions
    WHERE decision_date IS NOT NULL
    GROUP BY yr
    ORDER BY yr DESC
    LIMIT :limit
""")


_CUSTOMER_TREND_ALL = _trend_sql("customer_pib = :pib")

_CUSTOMER_TREND_BY_NAME_ALL = _trend_sql(f"{_CUSTOMER_NAME_KEY} = :name")

# --------------------------------------------------------------------------- #
#  SQL — the latest decisions of the customer (the incumbent in the tender card, A4)
# --------------------------------------------------------------------------- #

# “Who used to win from this customer” — not an aggregate, but a FEED of the latest
# decisions: winner, amount, date. The key is the same ``_CUSTOMER_NAME_KEY`` as in the
# profile: the block stands on one page with “Customer analytics”, and if the
# samples diverged, two blocks about one customer would contradict each other.
#
# ‼️ The unit of the feed is the DECISION (tender+winner+date), and not a table row.
# In ``tender_awards`` one row = one LOT, and the amount in ``award_amount_rsd``
# is NOT per lot: it is repeated on each row of the decision. Verified on the live
# production — of 131 multi-lot groups (397 rows, 27% of the table) the amount is constant
# within the group in ALL 131, with not one with different amounts. Hence two consequences:
#   * a feed by rows would show “AMICUS SRB · 8,285,300 RSD · 03.07.2026”
#     five times in a row (lots 8–12 of one tender) — it reads as a breakage;
#   * the lot amounts must not be summed — that is a double count (5 lots × 8.3 M = 41 M
#     instead of 8.3 M). Therefore ``max()``, not ``sum()``: inside the group the value
#     is one, max() merely fetches it, ignoring the NULL lots.
# The number of lots is given in a separate column — “(5 lots)” honestly explains why
# one decision covers five positions.
#
# ``winner_name`` — is in the grouping key, not in the aggregate: for some rows
# ``winner_pib`` is empty (NULL), and grouping by the PIB alone would merge DIFFERENT
# PIB-less winners of one tender into one decision.
#
# Rows without ``decision_date`` — to the tail (NULLS LAST): “date unknown” must not
# push fresh decisions out of the top. ``max(id) DESC`` — a deterministic
# tie-break (the identity key): without it the decisions of one date would come back in
# an arbitrary order, and LIMIT would cut off different ones each time.
_CUSTOMER_RECENT_AWARDS_BY_NAME = text(f"""
    SELECT
        winner_name,
        winner_pib,
        decision_date,
        {_DECISION_RSD}   AS amount_rsd,
        {_DECISION_EUR}   AS amount_eur,
        count(*)                AS lots,
        min(lot_title)          AS lot_title
    FROM tender_awards
    WHERE {_CUSTOMER_NAME_KEY} = :name
    GROUP BY tender_id, winner_name, winner_pib, decision_date
    ORDER BY decision_date DESC NULLS LAST, max(id) DESC
    LIMIT :limit
""")

# --------------------------------------------------------------------------- #
#  SQL — the reference of the customer (the choice of the customer on the cabinet page)
# --------------------------------------------------------------------------- #

# Grouping by the NORMALIZED name (``_CUSTOMER_NAME_KEY``), while the PIB and the name itself are
# storefront ``mode()`` values.
# Why not by PIB: for 22.9% of rows ``customer_pib`` is empty, and the same
# the customer would split into “rows with a PIB” and “rows without a PIB” — the counter in the list
# would diverge from the profile the link leads to. The price of the name key: two different
# customers with literally the same name would merge into one; with an empty PIB, for
# every fifth one there is nothing to tell them apart anyway, and an undercount is worse than a merge.
#
# ‼️ This predicate MUST match the sample of the profile
# (``_CUSTOMER_SUMMARY_BY_NAME_ALL``): the list promises N decisions — the profile must
# show the same N. Hence also the call ``customer_award_stats(customer_name=...)``
# WITHOUT ``customer_pib`` in the page service (the project rules: one predicate per meaning).
# For the same reason the grouping key is the normalized one: the profile matches by
# ``_CUSTOMER_NAME_KEY``, and if the list grouped by the verbatim name, "ZAVOD …
# SABAC" would stand in the list twice (21 and 9), while the profile would show for each row
# the same 30.
# The storefront name of a group is the PREVAILING spelling (mode()), not an arbitrary one:
# a group by construction contains different variants of one name, and min/max would pick the
# lexicographic one, i.e. with equal success the variant with a double space.
# The PIB of the group is also the MOST FREQUENT one, not max(): max() over text gives
# the lexicographic “winner” ('99…' > '100236236'), i.e. an arbitrary PIB from the
# group. mode() ignores NULL and takes the prevailing one — if a name has several
# PIBs (data noise), we show the one behind the majority of the rows.
# ``awards`` — the number of DECISIONS, not of lot rows (see ``decisions_cte``): the counter
# counts only the first row of each decision. A decision = (customer key, tender,
# winner, name — for PIB-less ones), the same partition as the ``decisions`` of the profile.
_CUSTOMER_DIRECTORY_COLS = """
        mode() WITHIN GROUP (ORDER BY customer_name) AS customer_name,
        mode() WITHIN GROUP (ORDER BY customer_pib) AS customer_pib,
        count(*) FILTER (WHERE first_of_decision)   AS awards,
        max(decision_date)  AS last_date
"""

# Sorting of the reference. ‼️ ORDER BY is assembled ONLY from this whitelist
# (the key — the external ``?sort=``, the value — a module constant): not a single character from the user
# gets into the SQL, an unknown key → the default (``directory_sort``).
# The tie-break everywhere is the name: without it the order of equal rows drifts between pages,
# and the pagination duplicates/loses rows at the joint.
DIRECTORY_SORT_DEFAULT = "decisions_desc"
_DIRECTORY_ORDER_BY = {
    "decisions_desc": "awards DESC, customer_name",
    "last_desc": "last_date DESC NULLS LAST, awards DESC, customer_name",
    "name_asc": "customer_name ASC, awards DESC",
}
DIRECTORY_SORT_KEYS: tuple[str, ...] = tuple(_DIRECTORY_ORDER_BY)

# ``count(*) OVER ()`` — the number of groups BEFORE LIMIT/OFFSET (for pagination without a second
# query). ``:offset`` — a bind, the same as ``:limit``.
_CUSTOMER_DIRECTORY_TOTAL_COL = ", count(*) OVER () AS total"


def directory_sort(sort: str | None) -> str:
    """The external ``?sort=`` → a key of the whitelist; an unknown/empty one → the default."""
    return sort if sort in _DIRECTORY_ORDER_BY else DIRECTORY_SORT_DEFAULT


def _directory_sql(order_key: str, *, search: bool):
    having = (
        # ILIKE — by the KEY (see below), PIB — by PREFIX: a substring of digits would be noisy
        # (“1” would find half the database). HAVING, not WHERE: the key is constant in the group, while
        # the PIB is not; a WHERE by PIB would throw the rows without a PIB out of the group, and the counter
        # would diverge from the profile. bool_or — “at least one row of the group”.
        f"""
    HAVING {_CUSTOMER_NAME_FOLDED} LIKE CAST(:term AS text) ESCAPE '\\'
        OR bool_or(customer_pib LIKE CAST(:pib_term AS text) ESCAPE '\\')"""
        if search
        else ""
    )
    # The window function is in the nested SELECT: an aggregate over the group cannot count "the first
    # row of the decision", and PARTITION BY matches the partition of ``decisions``.
    return text(f"""
    SELECT {_CUSTOMER_DIRECTORY_COLS}{_CUSTOMER_DIRECTORY_TOTAL_COL}
    FROM (
        SELECT
            tender_awards.*,
            row_number() OVER (
                PARTITION BY {_CUSTOMER_NAME_KEY}, tender_id, winner_pib,
                             CASE WHEN winner_pib IS NULL THEN winner_name END
                ORDER BY id
            ) = 1 AS first_of_decision
        FROM tender_awards
        WHERE customer_name IS NOT NULL AND btrim(customer_name) <> ''
    ) AS lot_rows
    GROUP BY {_CUSTOMER_NAME_KEY}{having}
    ORDER BY {_DIRECTORY_ORDER_BY[order_key]}
    LIMIT :limit OFFSET :offset
""")


# Without a search — the most active/latest/alphabetical; with a search — a separate SQL
# (and not an optional :term in the common one): the same asyncpg pitfall with a parameter that
# "is there, is not" as with the ALL/WINDOW window above. ESCAPE '\' — so that '%' and '_' in the input
# are searched literally, and not as a mask.
# ‼️ ILIKE — by the KEY, not by the verbatim name: cutting the rows BEFORE grouping
# would throw the variants with another spelling out of the group and return “21 decisions”
# where the profile would show 30.
_DIRECTORY_SQL = {
    (k, search): _directory_sql(k, search=search)
    for k in DIRECTORY_SORT_KEYS
    for search in (False, True)
}
_CUSTOMER_DIRECTORY_TOP = _DIRECTORY_SQL[(DIRECTORY_SORT_DEFAULT, False)]
_CUSTOMER_DIRECTORY_SEARCH = _DIRECTORY_SQL[(DIRECTORY_SORT_DEFAULT, True)]

# --------------------------------------------------------------------------- #
#  SQL — the competitor profile (by winner_pib)
# --------------------------------------------------------------------------- #


# The competitor profile: the unit is the decision (tender × customer) where the winner
# = ``:pib``. The lots of one decision are collapsed the same way as for the customer (the amount — max).
# The customer is part of the key: a tender belongs to one customer, therefore the partition
# matches “one decision per tender”, but grouping by customer is needed for the top.
def _competitor_decisions_cte(where: str) -> str:
    return f"""WITH decisions AS (
        SELECT
            tender_id,
            customer_name,
            customer_pib,
            {_DECISION_RSD}   AS amount_rsd,
            {_DECISION_EUR}   AS amount_eur
        FROM tender_awards
        WHERE {where}
        GROUP BY tender_id, customer_name, customer_pib
    )"""


_COMPETITOR_SUMMARY_ALL = text(f"""{_competitor_decisions_cte("winner_pib = :pib")}
    SELECT
        count(*)                                                        AS total_wins,
        count(DISTINCT customer_pib) FILTER (WHERE customer_pib IS NOT NULL) AS distinct_customers,
        count(amount_rsd)                                               AS with_amount,
        COALESCE(sum(amount_rsd), 0)                                    AS total_rsd,
        COALESCE(sum(amount_eur), 0)                                    AS total_eur
    FROM decisions
""")

_COMPETITOR_SUMMARY_WINDOW = text(
    f"""{_competitor_decisions_cte("winner_pib = :pib AND decision_date >= :cutoff")}
    SELECT
        count(*)                                                        AS total_wins,
        count(DISTINCT customer_pib) FILTER (WHERE customer_pib IS NOT NULL) AS distinct_customers,
        count(amount_rsd)                                               AS with_amount,
        COALESCE(sum(amount_rsd), 0)                                    AS total_rsd,
        COALESCE(sum(amount_eur), 0)                                    AS total_eur
    FROM decisions
"""
)

_COMPETITOR_TOP_CUSTOMERS_ALL = text(
    f"""{_competitor_decisions_cte("winner_pib = :pib")}
    SELECT
        customer_name,
        customer_pib,
        count(*)                            AS wins,
        COALESCE(sum(amount_rsd), 0)        AS sum_rsd
    FROM decisions
    WHERE customer_pib IS NOT NULL
    GROUP BY customer_name, customer_pib
    ORDER BY wins DESC, sum_rsd DESC
    LIMIT :limit
"""
)

_COMPETITOR_TOP_CUSTOMERS_WINDOW = text(
    f"""{_competitor_decisions_cte("winner_pib = :pib AND decision_date >= :cutoff")}
    SELECT
        customer_name,
        customer_pib,
        count(*)                            AS wins,
        COALESCE(sum(amount_rsd), 0)        AS sum_rsd
    FROM decisions
    WHERE customer_pib IS NOT NULL
    GROUP BY customer_name, customer_pib
    ORDER BY wins DESC, sum_rsd DESC
    LIMIT :limit
"""
)


# --------------------------------------------------------------------------- #
#  Concentration constants
# --------------------------------------------------------------------------- #

MONOPOLY_TOP1_PCT = (
    70  # the “monopoly” threshold of one supplier (the share of top-1, %)
)

# --------------------------------------------------------------------------- #
#  DTO
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class WinnerRow:
    """A row of the top winners of the customer."""

    name: str | None
    pib: str | None
    wins: int
    sum_rsd: Decimal


@dataclass(frozen=True)
class RecentAwardRow:
    """One decision of the contracting authority: who was awarded, at what price and when.

    The amounts keep NULL (``None`` ≠ ``0``): for an award row the amount appears
    only after the contract is registered, and “0 RSD” instead of “amount not published”
    would be a lie (the same reason as for ``_as_decimal_or_none``).
    """

    winner_name: str | None
    winner_pib: str | None
    amount_rsd: Decimal | None
    amount_eur: Decimal | None
    decision_date: date | None
    lot_title: str | None = None
    lots: int = 1

    @property
    def is_multilot(self) -> bool:
        """A decision covers several lots of one tender (the amount — per decision)."""
        return self.lots > 1

    @property
    def is_joint(self) -> bool:
        """A decision in favour of a consortium (several winners in one row).

        Known data debt: 7 rows out of 1454 have in ``winner_pib`` a concatenation of PIBs
        through a comma (“101470969, 103229839”), and in ``winner_name`` the same kind of
        concatenation of names. This is not garbage and not a broken PIB — it is a joint bid,
        which the parser cannot yet split into the individual winners.
        """
        return bool(self.winner_pib and "," in self.winner_pib)

    @property
    def single_pib(self) -> str | None:
        """The winner’s PIB, when there is ONE; for a consortium — None.

        A concatenation must not be shown as a PIB: “101470969, 103229839” is not an identifier
        and will not be found in any register.
        """
        if not self.winner_pib or self.is_joint:
            return None
        return self.winner_pib


@dataclass(frozen=True)
class DirectoryRow:
    """A row of the reference of the customer (the choice of the customer): the name + the volume of its decisions."""

    name: str
    pib: str | None
    awards: int
    last_date: date | None


@dataclass(frozen=True)
class CustomerStats:
    """The profile of the contracting authority: the activity of awards and the top winners.

    ``avg_rsd``/``median_rsd`` — over the rows with a known amount (their ``with_amount``,
    not ``total_awards``); None if there are no amounts at all. ``first_date``/``last_date``/
    ``without_date`` describe the PERIOD of the sample — an aggregate without them is not interpretable.
    """

    customer_pib: str
    total_awards: int
    distinct_winners: int
    with_amount: int
    total_rsd: Decimal
    total_eur: Decimal
    top_winners: list[WinnerRow] = field(default_factory=list)
    months: int | None = None
    avg_rsd: Decimal | None = None
    median_rsd: Decimal | None = None
    first_date: date | None = None
    last_date: date | None = None
    without_date: int = 0

    @property
    def has_data(self) -> bool:
        return self.total_awards > 0

    @property
    def concentration_pct(self) -> int | None:
        """The share of the top-1 winner in the total number of decisions (an indicator of “being fed”).

        None if there are no decisions. A high value with a small distinct_winners —
        a signal that one supplier consistently wins with the customer.
        """
        if self.total_awards <= 0 or not self.top_winners:
            return None
        return round(self.top_winners[0].wins * 100 / self.total_awards)

    @property
    def cr3_pct(self) -> int | None:
        """The share of the top-3 winners in the total number of decisions. None if there are no decisions."""
        if self.total_awards <= 0 or not self.top_winners:
            return None
        top3_wins = sum(w.wins for w in self.top_winners[:3])
        return round(top3_wins * 100 / self.total_awards)

    @property
    def is_monopolized(self) -> bool:
        """True if one supplier dominates (distinct_winners==1 or top-1 >= MONOPOLY_TOP1_PCT)."""
        if self.distinct_winners == 1:
            return True
        conc = self.concentration_pct
        return conc is not None and conc >= MONOPOLY_TOP1_PCT


@dataclass(frozen=True)
class YearRow:
    """A row of the yearly dynamics of the customer’s procurement (one per calendar year)."""

    year: int
    awards: int
    distinct_winners: int
    total_rsd: Decimal
    total_eur: Decimal


@dataclass(frozen=True)
class CustomerRow:
    """A row of the top customers of a competitor."""

    name: str | None
    pib: str | None
    wins: int
    sum_rsd: Decimal


@dataclass(frozen=True)
class CompetitorStats:
    """The competitor profile: how much and with whom it wins."""

    winner_pib: str
    total_wins: int
    distinct_customers: int
    with_amount: int
    total_rsd: Decimal
    total_eur: Decimal
    top_customers: list[CustomerRow] = field(default_factory=list)
    months: int | None = None

    @property
    def has_data(self) -> bool:
        return self.total_wins > 0


# --------------------------------------------------------------------------- #
#  Queries
# --------------------------------------------------------------------------- #


def _cutoff(months: int) -> datetime:
    """The window boundary: now() - months (approximately 30 days/month, UTC-aware)."""
    return datetime.now(timezone.utc) - timedelta(days=30 * months)


def _as_decimal(value: Any) -> Decimal:
    if value is None:
        return Decimal("0")
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


def _as_decimal_or_none(value: Any) -> Decimal | None:
    """Like ``_as_decimal``, but keeps NULL: “amounts unknown” ≠ “amount 0”.

    avg/median over rows without amounts give NULL — replacing it with zero would mean drawing
    an “average check of 0 RSD” where the check has simply not been published yet.
    """
    if value is None:
        return None
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


def _as_int(value: Any) -> int:
    return int(value) if value is not None else 0


def _like_term(term: str) -> str:
    """User input → a safe ILIKE mask ``%term%``.

    We escape the escape character itself and the ILIKE masks, otherwise the input “100%” would search
    for "100<anything>“, and ”_" — any character. The order of replacements matters: the backslash first.
    """
    escaped = term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


RECENT_AWARDS_LIMIT = 8  # how many of the latest decisions we show in the tender card


async def customer_recent_awards(
    session: Any, *, customer_name: str, limit: int = RECENT_AWARDS_LIMIT
) -> list[RecentAwardRow]:
    """The latest decisions of the customer ``customer_name`` (the feed for the tender card).

    Keyed ONLY by name, without the pib branch: on the subset of tenders with an award, 100% of rows
    (1454 out of 1454) have an empty ``customer_tax_id`` — a pib branch would be code
    that never runs. Matching is by ``_CUSTOMER_NAME_KEY``, so a name with extra spaces/in another case finds the same history.

    An empty/whitespace name → ``[]``, not a query: the normalized "" would match the
    key of the rows with an empty name and would assemble a feed from other people’s decisions.
    """
    key = normalize_customer_name(customer_name or "")
    if not key:
        return []

    rows = (
        await session.execute(
            _CUSTOMER_RECENT_AWARDS_BY_NAME, {"name": key, "limit": limit}
        )
    ).fetchall()
    return [
        RecentAwardRow(
            winner_name=getattr(r, "winner_name", None),
            winner_pib=getattr(r, "winner_pib", None),
            amount_rsd=_as_decimal_or_none(getattr(r, "amount_rsd", None)),
            amount_eur=_as_decimal_or_none(getattr(r, "amount_eur", None)),
            decision_date=getattr(r, "decision_date", None),
            lot_title=getattr(r, "lot_title", None),
            lots=_as_int(getattr(r, "lots", 1)) or 1,
        )
        for r in rows
    ]


@dataclass(frozen=True)
class DirectoryPage:
    """The reference page: the rows + how many groups in total (before LIMIT/OFFSET)."""

    rows: list[DirectoryRow]
    total: int


async def search_customers_page(
    session: Any,
    *,
    term: str | None = None,
    limit: int = 20,
    offset: int = 0,
    sort: str | None = None,
) -> DirectoryPage:
    """The customer reference for the choice on the cabinet page (sorting + pages).

    Without ``term`` — the whole reference; with ``term`` — a substring ILIKE by name OR
    a PIB prefix. ``sort`` — a key from ``DIRECTORY_SORT_KEYS`` (otherwise the default).
    The row key is the NAME (see the comment to ``_CUSTOMER_DIRECTORY_COLS``): the profile
    to which the link leads counts with exactly the same predicate, therefore the number of decisions in
    the list and in the profile match.
    """
    cleaned = (term or "").strip()
    params: dict[str, Any] = {"limit": limit, "offset": max(0, offset)}
    if cleaned:
        # The input is normalized by the same formula as the column: the ILIKE goes by the KEY
        # (lower case, single spaces), therefore “Opstina   Novi Sad (Cyrillic, as in the DB)” with
        # extra spaces must find “Opstina Novi Sad (Cyrillic)”.
        # Additionally we fold to Latin without diacritics (``fold``): the column
        # in the DB is in Cyrillic, the client types in Latin (beograd / čačak / dj).
        params["term"] = _like_term(
            re.sub(r"\s+", " ", fold(cleaned, ijekavica=False)).strip()
        )
        params["pib_term"] = _like_term(cleaned.replace(" ", ""))[1:]
    sql = _DIRECTORY_SQL[(directory_sort(sort), bool(cleaned))]

    rows = (await session.execute(sql, params)).fetchall()
    total = _as_int(getattr(rows[0], "total", len(rows))) if rows else 0
    return DirectoryPage(
        rows=[
            DirectoryRow(
                name=str(getattr(r, "customer_name", "") or ""),
                pib=getattr(r, "customer_pib", None),
                awards=_as_int(getattr(r, "awards", 0)),
                last_date=getattr(r, "last_date", None),
            )
            for r in rows
        ],
        total=total,
    )


async def search_customers(
    session: Any, *, term: str | None = None, limit: int = 20
) -> list[DirectoryRow]:
    """The first page of the reference by default (without pagination) — see
    :func:`search_customers_page`."""
    return (await search_customers_page(session, term=term, limit=limit)).rows


async def customer_award_stats(
    session: Any,
    *,
    customer_pib: str | None = None,
    customer_name: str | None = None,
    months: int | None = None,
    top_n: int = 5,
) -> CustomerStats:
    """The profile of the contracting authority by ``customer_pib`` OR (if the PIB is empty) ``customer_name``.

    Keying: with a non-empty ``customer_pib`` — by PIB; otherwise, with a non-empty
    ``customer_name`` — an exact match by name (in our database ~93% of tenders
    have a name but no PIB → the name fallback raises the coverage to ~100%).
    ``months=None`` — for all time; otherwise a window by ``decision_date``.
    Returns :class:`CustomerStats` (``has_data=False`` if there are no decisions).
    """
    by_name = not customer_pib and bool(customer_name)
    if by_name:
        # The parameter side is normalized by the same mirror as the column in the SQL.
        name_key = normalize_customer_name(customer_name or "")
        if months is None:
            summary_sql, winners_sql, params = (
                _CUSTOMER_SUMMARY_BY_NAME_ALL,
                _CUSTOMER_TOP_WINNERS_BY_NAME_ALL,
                {"name": name_key},
            )
        else:
            summary_sql, winners_sql, params = (
                _CUSTOMER_SUMMARY_BY_NAME_WINDOW,
                _CUSTOMER_TOP_WINNERS_BY_NAME_WINDOW,
                {"name": name_key, "cutoff": _cutoff(months)},
            )
    elif months is None:
        summary_sql, winners_sql, params = (
            _CUSTOMER_SUMMARY_ALL,
            _CUSTOMER_TOP_WINNERS_ALL,
            {"pib": customer_pib},
        )
    else:
        summary_sql, winners_sql, params = (
            _CUSTOMER_SUMMARY_WINDOW,
            _CUSTOMER_TOP_WINNERS_WINDOW,
            {"pib": customer_pib, "cutoff": _cutoff(months)},
        )

    srow = (await session.execute(summary_sql, params)).first()
    wrows = (await session.execute(winners_sql, {**params, "limit": top_n})).fetchall()

    top_winners = [
        WinnerRow(
            name=getattr(r, "winner_name", None),
            pib=getattr(r, "winner_pib", None),
            wins=_as_int(getattr(r, "wins", 0)),
            sum_rsd=_as_decimal(getattr(r, "sum_rsd", 0)),
        )
        for r in wrows
    ]

    return CustomerStats(
        customer_pib=customer_pib or "",
        total_awards=_as_int(getattr(srow, "total_awards", 0)) if srow else 0,
        distinct_winners=_as_int(getattr(srow, "distinct_winners", 0)) if srow else 0,
        with_amount=_as_int(getattr(srow, "with_amount", 0)) if srow else 0,
        total_rsd=_as_decimal(getattr(srow, "total_rsd", 0)) if srow else Decimal("0"),
        total_eur=_as_decimal(getattr(srow, "total_eur", 0)) if srow else Decimal("0"),
        top_winners=top_winners,
        months=months,
        avg_rsd=_as_decimal_or_none(getattr(srow, "avg_rsd", None)) if srow else None,
        median_rsd=(
            _as_decimal_or_none(getattr(srow, "median_rsd", None)) if srow else None
        ),
        first_date=getattr(srow, "first_date", None) if srow else None,
        last_date=getattr(srow, "last_date", None) if srow else None,
        without_date=_as_int(getattr(srow, "without_date", 0)) if srow else 0,
    )


async def customer_yearly_trend(
    session: Any,
    *,
    customer_pib: str | None = None,
    customer_name: str | None = None,
    years: int = 5,
) -> list[YearRow]:
    """The dynamics of the awards of the customer by year (the last ``years`` years).

    Keying — as in :func:`customer_award_stats`: by ``customer_pib``, otherwise
    (empty PIB) a fallback by the exact ``customer_name``. It shows whether the customer’s procurement
    volume grows and whether the circle of winners changes — the “over time” axis, which is absent from
    the static profile. Returns a list in ascending year order
    (chronologically, convenient for left-to-right rendering); empty if there is no data.
    """
    by_name = not customer_pib and bool(customer_name)
    if by_name:
        sql, params = (
            _CUSTOMER_TREND_BY_NAME_ALL,
            {"name": normalize_customer_name(customer_name or "")},
        )
    else:
        sql, params = _CUSTOMER_TREND_ALL, {"pib": customer_pib}

    rows = (await session.execute(sql, {**params, "limit": years})).fetchall()

    trend = [
        YearRow(
            year=_as_int(getattr(r, "yr", 0)),
            awards=_as_int(getattr(r, "awards", 0)),
            distinct_winners=_as_int(getattr(r, "distinct_winners", 0)),
            total_rsd=_as_decimal(getattr(r, "total_rsd", 0)),
            total_eur=_as_decimal(getattr(r, "total_eur", 0)),
        )
        for r in rows
    ]
    # The SQL returns the years in descending order (the last N) — we unfold them into chronology.
    trend.reverse()
    return trend


async def competitor_stats(
    session: Any, *, winner_pib: str, months: int | None = None, top_n: int = 5
) -> CompetitorStats:
    """The competitor profile by ``winner_pib``.

    ``months=None`` — for all time; otherwise a window by ``decision_date``.
    """
    if months is None:
        summary_sql, customers_sql, params = (
            _COMPETITOR_SUMMARY_ALL,
            _COMPETITOR_TOP_CUSTOMERS_ALL,
            {"pib": winner_pib},
        )
    else:
        cutoff = _cutoff(months)
        summary_sql, customers_sql, params = (
            _COMPETITOR_SUMMARY_WINDOW,
            _COMPETITOR_TOP_CUSTOMERS_WINDOW,
            {"pib": winner_pib, "cutoff": cutoff},
        )

    srow = (await session.execute(summary_sql, params)).first()
    crows = (
        await session.execute(customers_sql, {**params, "limit": top_n})
    ).fetchall()

    top_customers = [
        CustomerRow(
            name=getattr(r, "customer_name", None),
            pib=getattr(r, "customer_pib", None),
            wins=_as_int(getattr(r, "wins", 0)),
            sum_rsd=_as_decimal(getattr(r, "sum_rsd", 0)),
        )
        for r in crows
    ]

    return CompetitorStats(
        winner_pib=winner_pib,
        total_wins=_as_int(getattr(srow, "total_wins", 0)) if srow else 0,
        distinct_customers=(
            _as_int(getattr(srow, "distinct_customers", 0)) if srow else 0
        ),
        with_amount=_as_int(getattr(srow, "with_amount", 0)) if srow else 0,
        total_rsd=_as_decimal(getattr(srow, "total_rsd", 0)) if srow else Decimal("0"),
        total_eur=_as_decimal(getattr(srow, "total_eur", 0)) if srow else Decimal("0"),
        top_customers=top_customers,
        months=months,
    )
