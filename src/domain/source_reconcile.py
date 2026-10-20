"""Reconciliation with the source (M2 of monitoring): one “discrepancy” predicate and the alert text.

It is called by the reconciliation worker (``src/workers/source_reconcile.py`` — the alert) and the admin
(``/admin/sources`` — the “reconciliation” column). The admin computes the same in SQL
(``sources_admin._RECONCILE_SQL``) — the mirror is pinned by the test
``tests/domain/test_source_reconcile.py``.

Tolerance: ``max(2, 2% of the source’s number)`` — an absolute difference greater than the tolerance.
An excess on our side (our > source) is also a discrepancy: duplicates or foreign forms.
"""

from __future__ import annotations

from datetime import date

#: The tolerance in units and as a share of the source’s number.
TOLERANCE_ABS = 2
TOLERANCE_REL = 0.02
#: How many of the latest publication days we reconcile per pass (today’s is incomplete).
RECONCILE_DAYS = 3


def tolerance(source_count: int) -> float:
    return max(float(TOLERANCE_ABS), TOLERANCE_REL * source_count)


def is_mismatch(source_count: int, our_count: int) -> bool:
    return abs(source_count - our_count) > tolerance(source_count)


def build_reconcile_alert_text(
    *,
    source_key: str,
    source_label: str | None,
    day: date,
    source_count: int,
    our_count: int,
) -> str:
    """The alert text with numbers: what the source counted, what we have, where to look."""
    label = source_label or source_key
    diff = our_count - source_count
    return (
        f"⚠️ Сверка с источником «{label}» ({source_key}): за {day:%d.%m.%Y} "
        f"у источника {source_count}, у нас {our_count} (разница {diff:+d}, "
        f"допуск {tolerance(source_count):.0f}).\n"
        "Недобор — приём потерял окно (токен/страницы/429/блок IP); "
        "перебор — дубли или чужие формы. Смотреть логи ingest и /admin/sources."
    )
