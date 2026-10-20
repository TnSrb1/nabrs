"""Integration: the profile filters “Subject type” and “Customer: follow / exclude” (0046).

The predicates live in the SQL (``STAGE_A_SQL`` and its tender-centric copy ``PREVIEW_WHERE_A``) — they are not proved on mocks (``.claude/rules/tests-known-defects.md``). The test runs the REAL ``RoutingWorker.route_tender`` and ``preview_service.preview_count`` against a throwaway PostgreSQL: the delivery (the ``delivery_outbox`` rows) and the counter of the form must answer the same.

The customers in the expectations are literals from production (01.10.2026, the top of jnportal for 90 days).
Requires ``TEST_RLS_DATABASE_URL`` (a throwaway PG, NEVER the deploy production).
"""

from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from src.api.cabinet import preview_service
from src.workers.routing import RoutingWorker

TEST_URL = os.getenv("TEST_RLS_DATABASE_URL", "")
SUPER_URL = os.getenv("TEST_SUPERUSER_DATABASE_URL", TEST_URL)

pytestmark = pytest.mark.skipif(
    not TEST_URL,
    reason="TEST_RLS_DATABASE_URL не задан — интеграция фильтров профиля пропускается",
)

NOW = datetime.now(timezone.utc)
EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
EPS = "Акционарско друштво Електропривреда Србије, Београд"
VOJSKA = "Министарство одбране Републике Србије"


@pytest_asyncio.fixture
async def world():
    eng = create_async_engine(SUPER_URL, poolclass=NullPool, future=True)
    tid, uid, cid = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    key = f"pf-{uuid.uuid4().hex[:8]}"
    created: dict[str, list] = {"profiles": [], "tenders": []}
    async with eng.begin() as c:
        await c.execute(
            text(
                "INSERT INTO tenants (id, country_code, tier, status) "
                "VALUES (:id, 'RS', 'lite', 'trial')"
            ),
            {"id": tid},
        )
        await c.execute(
            text(
                "INSERT INTO users (id, tenant_id, locale, role) "
                "VALUES (:id, :tid, 'ru', 'admin')"
            ),
            {"id": uid, "tid": tid},
        )
        await c.execute(
            text(
                "INSERT INTO user_channels (id, user_id, channel, external_id, status) "
                "VALUES (:id, :uid, 'telegram', '424243', 'active')"
            ),
            {"id": cid, "uid": uid},
        )
        await c.execute(
            text(
                "INSERT INTO sources (source_key, enabled, enabled_at, baseline_at, "
                "country_codes) VALUES (:k, true, :ea, :ba, ARRAY['RS']::char(2)[])"
            ),
            {"k": key, "ea": NOW - timedelta(days=60), "ba": EPOCH},
        )

    class W:
        source_key = key

        async def profile(
            self,
            *,
            keywords=None,
            stop=None,
            match_all=False,
            contract_types=None,
            include=None,
            exclude=None,
        ):
            pid = uuid.uuid4()
            groups = [{"all_of": [k]} for k in (keywords or [])]
            async with eng.begin() as c:
                await c.execute(
                    text(
                        "INSERT INTO profiles (id, tenant_id, country_code, is_active, "
                        "match_all, enabled_source_keys, keywords_include, "
                        "keywords_exclude, contract_types, customers_include, "
                        "customers_exclude, delivery_channels, ai_analysis_level, "
                        "created_at) VALUES (:id, :tid, 'RS', true, :ma, :keys, "
                        "CAST(:kw AS jsonb), :stop, :ct, :inc, :exc, "
                        "ARRAY['telegram'], 'none', :ca)"
                    ),
                    {
                        "id": pid,
                        "tid": tid,
                        "ma": match_all,
                        "keys": [key],
                        "kw": json.dumps(groups),
                        "stop": stop,
                        "ct": contract_types,
                        "inc": include,
                        "exc": exclude,
                        "ca": NOW - timedelta(days=2),
                    },
                )
            created["profiles"].append(pid)
            self.payload = {
                "enabled_source_keys": [key],
                "match_all": match_all,
                "keywords_include": groups,
                "keywords_exclude": list(stop or []),
                "contract_types": list(contract_types or []),
                "customers_include": list(include or []),
                "customers_exclude": list(exclude or []),
            }
            return pid

        async def tender(self, *, title, contract_type=None, customer=None):
            tender_id = uuid.uuid4()
            async with eng.begin() as c:
                await c.execute(
                    text(
                        "INSERT INTO tenders_normalized (id, source_key, source_uid, "
                        "country_code, regions, status, deadline_at, first_seen_at, "
                        "title_normalized, contract_type, customer_name) VALUES "
                        "(:id, :k, :uid, 'RS', '{}', 'active', :dl, :fs, :t, :ct, :cn)"
                    ),
                    {
                        "id": tender_id,
                        "k": key,
                        "uid": tender_id.hex,
                        "dl": NOW + timedelta(days=20),
                        "fs": NOW - timedelta(hours=1),
                        "t": title,
                        "ct": contract_type,
                        "cn": customer,
                    },
                )
            created["tenders"].append(tender_id)
            return tender_id

        async def route(self, tender_id) -> int:
            worker = RoutingWorker(
                session_factory=async_sessionmaker(
                    eng, class_=AsyncSession, expire_on_commit=False
                )
            )
            await worker.route_tender(
                {"tender_id": str(tender_id), "change_kind": "INSERT"}
            )
            async with eng.connect() as c:
                return int(
                    (
                        await c.execute(
                            text(
                                "SELECT count(*) FROM delivery_outbox "
                                "WHERE tender_id = :t"
                            ),
                            {"t": tender_id},
                        )
                    ).scalar_one()
                )

        async def preview(self) -> int:
            maker = async_sessionmaker(eng, class_=AsyncSession)
            async with maker() as s:
                return (await preview_service.preview_count(s, self.payload)).count

    w = W()
    try:
        yield w
    finally:
        async with eng.begin() as c:
            await c.execute(
                text("DELETE FROM delivery_outbox WHERE profile_id = ANY(:p)"),
                {"p": created["profiles"]},
            )
            await c.execute(
                text("DELETE FROM profiles WHERE tenant_id = :t"), {"t": tid}
            )
            await c.execute(
                text("DELETE FROM tenders_normalized WHERE id = ANY(:t)"),
                {"t": created["tenders"]},
            )
            await c.execute(
                text("DELETE FROM sources WHERE source_key = :k"), {"k": key}
            )
            await c.execute(
                text("DELETE FROM user_channels WHERE user_id = :u"), {"u": uid}
            )
            await c.execute(text("DELETE FROM users WHERE id = :u"), {"u": uid})
            await c.execute(text("DELETE FROM tenants WHERE id = :t"), {"t": tid})
        await eng.dispose()


# --------------------------------------------------------------------------- #
#  contract_types
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_contract_types_keeps_only_listed_type(world):
    await world.profile(match_all=True, contract_types=["services"])
    services = await world.tender(title="usluge ciscenja", contract_type="services")
    goods = await world.tender(title="nabavka papira", contract_type="goods")
    unknown = await world.tender(title="nabavka bez tipa", contract_type=None)

    assert await world.route(services) == 1
    assert await world.route(goods) == 0
    # NULL on the tender — the restriction is not applicable (the NULL semantics of stage A).
    assert await world.route(unknown) == 1
    assert await world.preview() == 2


@pytest.mark.asyncio
async def test_empty_contract_types_is_no_filter(world):
    await world.profile(match_all=True, contract_types=[])
    goods = await world.tender(title="nabavka papira", contract_type="goods")

    assert await world.route(goods) == 1
    assert await world.preview() == 1


# --------------------------------------------------------------------------- #
#  customers_exclude
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_excluded_customer_is_cut_case_and_space_insensitive(world):
    await world.profile(match_all=True, exclude=[EPS])
    eps = await world.tender(title="nabavka uglja", customer=EPS)
    # The same customer in another case and with a double space.
    eps_variant = await world.tender(
        title="nabavka uglja 2",
        customer="АКЦИОНАРСКО  ДРУШТВО Електропривреда Србије, Београд ",
    )
    other = await world.tender(title="nabavka uglja 3", customer=VOJSKA)
    nameless = await world.tender(title="nabavka uglja 4", customer=None)

    assert await world.route(eps) == 0
    assert await world.route(eps_variant) == 0
    assert await world.route(other) == 1
    assert await world.route(nameless) == 1
    assert await world.preview() == 2


# --------------------------------------------------------------------------- #
#  customers_include — “follow”
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_followed_customer_passes_without_keyword_match(world):
    await world.profile(keywords=["video"], include=[EPS])
    followed = await world.tender(title="nabavka uglja", customer=EPS)
    stranger = await world.tender(title="nabavka uglja", customer=VOJSKA)
    on_topic = await world.tender(title="video nadzor", customer=VOJSKA)

    assert await world.route(followed) == 1
    assert await world.route(stranger) == 0
    assert await world.route(on_topic) == 1
    assert await world.preview() == 2


@pytest.mark.asyncio
async def test_follow_alone_is_a_topic(world):
    """A profile without words/CPV and without match_all, but with “follow” — matches the customer."""
    await world.profile(include=[EPS])
    followed = await world.tender(title="nabavka uglja", customer=EPS)
    stranger = await world.tender(title="nabavka uglja", customer=VOJSKA)

    assert await world.route(followed) == 1
    assert await world.route(stranger) == 0
    assert await world.preview() == 1


@pytest.mark.asyncio
async def test_follow_keeps_explicit_negatives(world):
    """Stop words, the subject type and “exclude” act on “follow” too."""
    await world.profile(
        keywords=["video"], stop=["ugalj"], contract_types=["services"], include=[EPS]
    )
    stop_word = await world.tender(
        title="nabavka ugalj", contract_type="services", customer=EPS
    )
    wrong_type = await world.tender(
        title="nabavka opreme", contract_type="goods", customer=EPS
    )
    ok = await world.tender(
        title="odrzavanje opreme", contract_type="services", customer=EPS
    )

    assert await world.route(stop_word) == 0
    assert await world.route(wrong_type) == 0
    assert await world.route(ok) == 1
    assert await world.preview() == 1


@pytest.mark.asyncio
async def test_exclude_beats_follow(world):
    await world.profile(match_all=True, include=[EPS], exclude=[EPS])
    eps = await world.tender(title="nabavka uglja", customer=EPS)

    assert await world.route(eps) == 0
    assert await world.preview() == 0
