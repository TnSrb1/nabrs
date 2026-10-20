"""“Enable in all templates” under the app role (FORCE RLS): only its own tenant.

The service :func:`source_banner.enable_everywhere` reads the templates via
``svc.list_profiles`` (which sets ``set_tenant``) and writes via the ORM. We check by the real SQL under ``tendersrb_app``: the source goes into ALL the live templates of its own tenant, the templates of a foreign one do not change, a repeat writes nothing.

Requires ``TEST_RLS_DATABASE_URL`` (a throwaway PG — the gate brings it up itself; NOT production).
Mutation: remove ``set_tenant``/the tenant filter — the test must turn red
(see docs/reports/TESTING_sources-banner.md).
"""

from __future__ import annotations

import asyncio
import os
import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from src.api.cabinet import source_banner

TEST_URL = os.getenv("TEST_RLS_DATABASE_URL", "")
SUPER_URL = os.getenv("TEST_SUPERUSER_DATABASE_URL", TEST_URL)

pytestmark = pytest.mark.skipif(
    not TEST_URL,
    reason="TEST_RLS_DATABASE_URL не задан — сервис «включить во всех» не проверен",
)

OLD_OPT = "2026-09-01T08:00:00+00:00"
NOW = datetime(2026, 10, 16, 10, 0, tzinfo=timezone.utc)


def _seed() -> dict:
    """Tenant A (two templates: a1 without the source, a2 with it), tenant B (b1 without it).

    The source — a unique key per run: repeated runs in one PG do not interfere.
    """

    async def go() -> dict:
        su = create_async_engine(SUPER_URL, poolclass=NullPool, future=True)
        source = f"sb_test_{uuid.uuid4().hex[:8]}"
        a, b = uuid.uuid4(), uuid.uuid4()
        ids = {"a": str(a), "b": str(b), "source": source}
        async with su.begin() as c:
            await c.execute(
                text(
                    "INSERT INTO sources (source_key, display_name, enabled) "
                    "VALUES (:k, 'Test', true)"
                ),
                {"k": source},
            )
            for tid in (a, b):
                await c.execute(
                    text(
                        "INSERT INTO tenants (id, country_code, tier, status) "
                        "VALUES (:id, 'RS', 'lite', 'trial')"
                    ),
                    {"id": tid},
                )
            for name, tid, keys in (
                ("a1", a, ["jnportal"]),
                ("a2", a, ["jnportal", source]),
                ("b1", b, ["jnportal"]),
            ):
                pid = uuid.uuid4()
                ids[name] = str(pid)
                await c.execute(
                    text(
                        "INSERT INTO profiles (id, tenant_id, name, is_active, "
                        "enabled_source_keys, source_opt_in) VALUES "
                        "(:id, :tid, :n, true, :keys, CAST(:opt AS jsonb))"
                    ),
                    {
                        "id": pid,
                        "tid": tid,
                        "n": name,
                        "keys": keys,
                        "opt": '{"jnportal": "%s"}' % OLD_OPT,
                    },
                )
        await su.dispose()
        return ids

    return asyncio.run(go())


async def _state(ids: dict) -> dict[str, tuple[list[str], dict]]:
    """The state of the three templates of the seed by their ids — by the superuser, bypassing the RLS."""
    su = create_async_engine(SUPER_URL, poolclass=NullPool, future=True)
    async with su.connect() as c:
        rows = (
            await c.execute(
                text(
                    "SELECT id, enabled_source_keys, source_opt_in FROM profiles "
                    "WHERE id = ANY(CAST(:ids AS uuid[]))"
                ),
                {"ids": [ids["a1"], ids["a2"], ids["b1"]]},
            )
        ).all()
    await su.dispose()
    by_id = {
        str(r.id): (list(r.enabled_source_keys), dict(r.source_opt_in)) for r in rows
    }
    return {name: by_id[ids[name]] for name in ("a1", "a2", "b1")}


async def _enable_as_app(
    tenant_id: str, source: str, when: datetime
) -> tuple[int, int]:
    """The call of the service via the app role: its own engine, the session factory as in the routes."""
    engine = create_async_engine(TEST_URL, poolclass=NullPool, future=True)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as db:
            return await source_banner.enable_everywhere(db, tenant_id, source, when)
    finally:
        await engine.dispose()


def test_enable_everywhere_touches_only_own_tenant_under_app_role():
    ids = _seed()
    before = asyncio.run(_state(ids))
    assert before["b1"][0] == ["jnportal"]

    total, with_key = asyncio.run(_enable_as_app(ids["a"], ids["source"], NOW))
    assert (total, with_key) == (2, 2)

    after = asyncio.run(_state(ids))
    assert after["a1"][0] == ["jnportal", ids["source"]]
    assert after["a1"][1]["jnportal"] == OLD_OPT
    assert after["a1"][1][ids["source"]] == NOW.isoformat()
    # A template where the source already stands: the subscription moment is the previous one, not overwritten.
    assert after["a2"] == before["a2"]
    # The foreign tenant is untouched.
    assert after["b1"] == before["b1"]


def test_second_enable_writes_nothing_under_app_role():
    ids = _seed()
    asyncio.run(_enable_as_app(ids["a"], ids["source"], NOW))
    once = asyncio.run(_state(ids))
    asyncio.run(_enable_as_app(ids["a"], ids["source"], NOW.replace(day=17)))
    assert asyncio.run(_state(ids)) == once
