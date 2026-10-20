"""A minimal async layer of SQLAlchemy 2.0 sessions.

There was no separate session module in the project before Sprint-15 (see PROJECT_BRIEF —
“the pattern is already in src/db/session.py if it exists, otherwise create a minimal one”).
Here we set up a lazy async engine and ``async_sessionmaker``: on importing the module
the network is not touched, the DB connection is created only at the first access via
:func:`get_engine`.

The URL is taken from the ENV ``DATABASE_URL`` (the asyncpg driver). The real value — in
``infra/secrets/*`` on the VPS (docs/archive/INFRA.md, ADR-016); there is no secret in the repository.

Usage in the handlers::

    from src.db.session import session_scope

    async with session_scope() as session:
        ...  # CRUD via AsyncSession
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from functools import lru_cache
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

# Transaction-local GUC setter — is_local=True reverts at COMMIT/ROLLBACK
# so pool connections never leak tenant context between requests.
_SET_TENANT_SQL = text("SELECT set_config('app.current_tenant_id', :tid, true)")

_TENANT_INFO_KEY = "current_tenant_id"

_DEFAULT_URL = "postgresql+asyncpg://localhost/tendersrb"


def _database_url() -> str:
    """The DB URL from the ENV ``DATABASE_URL`` (fallback — the local dev default)."""
    return os.getenv("DATABASE_URL", _DEFAULT_URL)


@lru_cache
def get_engine() -> AsyncEngine:
    """Lazily creates and caches the async engine.

    Called only at the first access to the DB, not at the module level — so that
    the import of the package and the unit tests do not require an available database.
    """
    return create_async_engine(_database_url(), pool_pre_ping=True, future=True)


@lru_cache
def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    """Lazily creates and caches the ``AsyncSession`` factory."""
    return async_sessionmaker(
        bind=get_engine(),
        class_=AsyncSession,
        expire_on_commit=False,
    )


async def set_current_tenant(session: AsyncSession, tenant_id: UUID | str) -> None:
    """Sets ``app.current_tenant_id`` transactionally for RLS (the GUC).

    ``is_local=true`` — SET LOCAL semantics: the value is rolled back at
    COMMIT/ROLLBACK, excluding a leak of the context between tenants on reuse
    of the connection pool.
    """
    await session.execute(_SET_TENANT_SQL, {"tid": str(tenant_id)})
    # We remember the tenant on the session: SET LOCAL dies on COMMIT, and reading further
    # in this same session must already be under it (see commit_keep_tenant).
    info = getattr(session, "info", None)
    if isinstance(info, dict):
        info[_TENANT_INFO_KEY] = str(tenant_id)


async def commit_keep_tenant(session: AsyncSession) -> None:
    """``commit()`` + restoration of the tenant for reading AFTER the commit.

    ``set_current_tenant`` — SET LOCAL: the value lives until the end of the transaction.
    Any read of a tenant-scoped table in the same session after ``commit()`` would go
    without a tenant and fall on the RLS policy (``invalid input syntax for type
    uuid: ""`` — a 500 after the confirmation of the email channel, audit 07.10.2026).
    A session without a remembered tenant (``set_current_tenant`` was not called) simply
    commits, as before.
    """
    await session.commit()
    info = getattr(session, "info", None)
    tenant_id = info.get(_TENANT_INFO_KEY) if isinstance(info, dict) else None
    if isinstance(tenant_id, str):
        await session.execute(_SET_TENANT_SQL, {"tid": tenant_id})


async def restore_tenant(session: AsyncSession) -> None:
    """Restore the remembered tenant after ``rollback()`` (SET LOCAL dies with it).

    A session without a remembered tenant — no-op.
    """
    info = getattr(session, "info", None)
    tenant_id = info.get(_TENANT_INFO_KEY) if isinstance(info, dict) else None
    if isinstance(tenant_id, str):
        await session.execute(_SET_TENANT_SQL, {"tid": tenant_id})


@asynccontextmanager
async def session_scope() -> AsyncIterator[AsyncSession]:
    """An async context manager that yields an ``AsyncSession``.

    The session is closed automatically on exiting the context. The commit/rollback is
    the responsibility of the calling code (the CRUD functions do ``commit`` explicitly).
    """
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        yield session
