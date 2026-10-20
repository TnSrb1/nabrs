"""VG-07 phase-1 — the reprocurement forecast (reprocurement forecast).

A read-only SELECT over ``tender_awards``: it searches for contracts whose
``contract_date + duration_months`` expires in the nearest ``horizon_months`` months.
Rows without both fields simply do not get into the sample (graceful-empty).
No migrations, no workers, no writes.

Keying — as in awards_analytics (the same ``_CUSTOMER_NAME_KEY``, and not its own
copy of the predicate):
- by ``customer_pib`` if set;
- otherwise by the NORMALIZED ``customer_name`` (the name fallback).

Two SQL variants (by_pib / by_name) instead of one with an optional parameter
— the same principle as in awards_analytics, to avoid the asyncpg pitfall
with an undefined type of an optional parameter.
The parameter :horizon goes through CAST(:horizon AS integer) so that asyncpg
does not confuse the type when passing it to make_interval.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import text

from src.analytics.awards_analytics import _CUSTOMER_NAME_KEY, normalize_customer_name

# --------------------------------------------------------------------------- #
#  SQL
# --------------------------------------------------------------------------- #


#: The contract end date. The ONLY definition: it is used both by the reprocurement forecast
#: and by the preset “ugovori koji ističu” of the report builder.
def expiry_expr(alias: str = "") -> str:
    """The expression of the end date; ``alias`` — the table qualifier (with a JOIN)."""
    p = f"{alias}." if alias else ""
    return f"({p}contract_date + make_interval(months => {p}duration_months))"


EXPIRY_EXPR = expiry_expr()


def expiry_window_sql(expr: str, horizon_param: str) -> str:
    """The predicate “expires within the nearest N months”: ``[today; today + N months]``.

    ``expr`` — the expression/column with the end date (a module-level literal, not user
    input), ``horizon_param`` — the name of the bind parameter with the number of months
    (``CAST`` — otherwise asyncpg will not infer the type for ``make_interval``).
    """
    return (
        f"{expr} >= CURRENT_DATE AND {expr} <= "
        f"(CURRENT_DATE + make_interval(months => CAST(:{horizon_param} AS integer)))"
    )


_REPRO_BY_PIB = text(f"""
    SELECT
        customer_pib,
        customer_name,
        winner_name,
        lot_title,
        award_amount_rsd,
        decision_date,
        {EXPIRY_EXPR} AS expiry_date
    FROM tender_awards
    WHERE contract_date IS NOT NULL
      AND duration_months IS NOT NULL
      AND customer_pib = :pib
      AND {expiry_window_sql(EXPIRY_EXPR, "horizon")}
    ORDER BY {EXPIRY_EXPR} ASC
    LIMIT :limit
""")

_REPRO_BY_NAME = text(f"""
    SELECT
        customer_pib,
        customer_name,
        winner_name,
        lot_title,
        award_amount_rsd,
        decision_date,
        {EXPIRY_EXPR} AS expiry_date
    FROM tender_awards
    WHERE contract_date IS NOT NULL
      AND duration_months IS NOT NULL
      AND {_CUSTOMER_NAME_KEY} = :name
      AND {expiry_window_sql(EXPIRY_EXPR, "horizon")}
    ORDER BY {EXPIRY_EXPR} ASC
    LIMIT :limit
""")

# --------------------------------------------------------------------------- #
#  DTO
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class ReprocurementRow:
    """One expected reprocurement."""

    customer_pib: str | None
    customer_name: str | None
    lot_title: str | None
    winner_name: str | None
    award_amount_rsd: Decimal | None
    decision_date: date | None
    expiry_date: date
    months_until_expiry: int


# --------------------------------------------------------------------------- #
#  Helpers
# --------------------------------------------------------------------------- #


def _as_decimal_opt(value: Any) -> Decimal | None:
    if value is None:
        return None
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


def _to_date(value: Any) -> date | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return None


def months_until(expiry: date, today: date) -> int:
    """The whole number of months between today and expiry (≥ 0).

    Used for formatting “~N months” in the rendering.
    Exported for unit tests.
    """
    delta = (expiry - today).days
    return max(0, round(delta / 30.44))


# --------------------------------------------------------------------------- #
#  Query
# --------------------------------------------------------------------------- #


async def get_reprocurement_forecast(
    session: Any,
    *,
    customer_pib: str | None = None,
    customer_name: str | None = None,
    horizon_months: int = 12,
    limit: int = 10,
) -> list[ReprocurementRow]:
    """The contracts of the contracting authority expiring within the nearest ``horizon_months`` months.

    Returns ``[]`` if there are no rows with the filled ``contract_date`` +
    ``duration_months`` — graceful-empty, does not fail.
    Keying: ``customer_pib`` takes priority; with an empty PIB — by ``customer_name``.
    """
    today = date.today()
    by_name = not customer_pib and bool(customer_name)
    if by_name:
        sql = _REPRO_BY_NAME
        params: dict[str, Any] = {
            # Normalization — by the same mirror as the column (see awards_analytics):
            # the predicate “the same rows of this customer” as in the profile must
            # select the same rows here too.
            "name": normalize_customer_name(customer_name or ""),
            "horizon": horizon_months,
            "limit": limit,
        }
    else:
        sql = _REPRO_BY_PIB
        params = {
            "pib": customer_pib,
            "horizon": horizon_months,
            "limit": limit,
        }

    rows = (await session.execute(sql, params)).fetchall()

    result: list[ReprocurementRow] = []
    for r in rows:
        expiry = _to_date(getattr(r, "expiry_date", None))
        if expiry is None:
            continue
        result.append(
            ReprocurementRow(
                customer_pib=getattr(r, "customer_pib", None),
                customer_name=getattr(r, "customer_name", None),
                lot_title=getattr(r, "lot_title", None),
                winner_name=getattr(r, "winner_name", None),
                award_amount_rsd=_as_decimal_opt(getattr(r, "award_amount_rsd", None)),
                decision_date=_to_date(getattr(r, "decision_date", None)),
                expiry_date=expiry,
                months_until_expiry=months_until(expiry, today),
            )
        )
    return result
