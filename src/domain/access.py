"""The SSOT predicate of a tenant's access activity/freeze (FIX-SG-01).

Single source of truth on the ``access_until`` axis:

    access_active(t) := (t.access_until IS NULL) OR (t.access_until > now())
    is_frozen(t)     := NOT access_active(t)
    extend_until(t, N) := max(t.access_until, now()) + N days   ← extension

The ``status IN ('trial', 'active')`` axis is a SEPARATE activity and is NOT part of
``is_frozen`` (see ``src/workers/matching.py``: in ``STAGE_A_SQL`` the ``status`` axis and the
``access_until`` axis are independent). ONLY the ``access_until`` axis lives here.

``now`` is passed explicitly (tz-aware UTC) — for testability and consistency with
the calling code, without a hidden ``datetime.now()`` inside the predicate.

A pure module: no network/DB/IO. The SQL helper assembles a fragment string of the same
predicate for use as a “brick” in ``WHERE`` expressions (asyncpg/text),
and does not touch the DB itself.
"""

from __future__ import annotations

from datetime import datetime, timedelta

__all__ = [
    "access_active",
    "is_frozen",
    "access_active_sql",
    "extend_until",
    "extend_until_sql",
]


def access_active(access_until: datetime | None, now: datetime) -> bool:
    """Access is active: unlimited (``None``) OR the term has not yet expired (``> now``).

    The boundary is strict: ``access_until == now`` is treated as expired (frozen),
    which is consistent with the SQL fragment ``access_until > now()``.
    """
    return access_until is None or access_until > now


# Free extension after access ends (owner decision 10.10.2026): a request
# from the cabinet banner carries this marker in ``trial_requests.comment`` (no migration needed),
# approval is not more than ``EXTENSION_MAX_DAYS`` days, monotonic (``extend_until``).
EXTENSION_REQUEST_PREFIX: str = "extension_request"
EXTENSION_MAX_DAYS: int = 30


# A request to re-register after self-deletion (owner 10.10, #658): the same technique —
# a marker in ``trial_requests.comment`` instead of a new table. Until the decision the comment
# carries ``reregister_request:<lang>:<email>``; after the decision the address is wiped.
REREGISTER_REQUEST_PREFIX: str = "reregister_request"


def is_reregister_request(comment: str | None) -> bool:
    """The request is a re-registration request (not an extension/upgrade)."""
    return comment is not None and comment.startswith(REREGISTER_REQUEST_PREFIX)


def is_extension_request(comment: str | None) -> bool:
    """The request was created by the “free extension” button (a marker in the comment)."""
    return comment is not None and comment.startswith(EXTENSION_REQUEST_PREFIX)


def clamp_extension_days(comment: str | None, days: int) -> int:
    """The approval term of an extension request is capped at ``EXTENSION_MAX_DAYS``."""
    if is_extension_request(comment):
        return min(days, EXTENSION_MAX_DAYS)
    return days


def is_frozen(access_until: datetime | None, now: datetime) -> bool:
    """Access is frozen: a term is set AND has expired (``<= now``). The inversion of ``access_active``."""
    return not access_active(access_until, now)


def extend_until(access_until: datetime | None, days: int, now: datetime) -> datetime:
    """The new term when extending by ``days``: AN EXTENSION DOES NOT REDUCE access.

    The base of the count is the GREATER of two points: the current term and ``now``::

        base  = max(access_until, now)   # access_until, if it is in the future
        until = base + days

    Why not ``now() + days``: “N days” in the admin/bot is an EXTENSION, not
    setting. An unconditional ``now() + days`` with any value smaller than the remainder silently
    cuts already paid access — a real incident of 04.08.2026: for a tenant with
    access until 18.08 “Extend by 3 days” was pressed, and the term became 07.08 (−11 days),
    with an active subscription until 18.08. Shortening a term remains possible, but
    only by an EXPLICIT path — ``revoke_access`` (“Freeze”).

    ``access_until IS NULL`` is treated as “no term issued” → the base is ``now``
    (the same as the SQL side of extension does: ``COALESCE(access_until, now())``
    in ``app_referral_grant_bonus``, 0023). This does NOT contradict
    :func:`access_active`, where NULL = “unlimited”: a fresh tenant with
    ``status='pending_verification'`` is created exactly with ``access_until IS NULL``
    (``api/cabinet/auth_service``), and approving the request must give it
    a finite term. The operator sees the current state next to the extension form,
    so the choice of base is not hidden from them.

    A pure function: ``now`` is passed explicitly (tz-aware UTC), as in
    :func:`access_active`. The SQL mirror is :func:`extend_until_sql`.
    """
    base = access_until if access_until is not None and access_until > now else now
    return base + timedelta(days=days)


def access_active_sql(alias: str) -> str:
    """An SQL fragment of the same predicate on the ``access_until`` axis for the table ``alias``.

    Returns the string::

        (<alias>.access_until IS NULL OR <alias>.access_until > now())

    The same fragment as in ``matching.STAGE_A_SQL`` (the access axis) — the matching
    behavior does NOT change, the helper only removes the duplication of the predicate.
    """
    return f"({alias}.access_until IS NULL OR {alias}.access_until > now())"


def extend_until_sql(alias: str, days_param: str) -> str:
    """The SQL mirror of :func:`extend_until` for ``UPDATE tenants SET access_until = …``.

    Returns the expression::

        GREATEST(COALESCE(<alias>.access_until, now()), now())
            + (CAST(<days_param> AS int) * INTERVAL '1 day')

    Needed where the current term IS UNKNOWN IN PYTHON: the expression reads the old
    value of the column in the same ``UPDATE``, so there is no extra SELECT (and no race between
    read and write). ``days_param`` is the name of the bind parameter (``":days"``);
    asyncpg requires an explicit ``CAST`` in ``text()``.

    The same meaning as in ``app_referral_grant_bonus`` (0023): the extension is counted
    from the greater of the dates and does not cut already granted access.
    """
    return (
        f"GREATEST(COALESCE({alias}.access_until, now()), now()) "
        f"+ (CAST({days_param} AS int) * INTERVAL '1 day')"
    )
