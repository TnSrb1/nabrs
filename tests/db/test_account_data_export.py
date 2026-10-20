"""“Download my data” THROUGH THE ROUTE under ``tendersrb_app`` (FORCE RLS), LEG-06.

It skips without ``TEST_RLS_DATABASE_URL`` (a throwaway PG, NEVER the deploy production).
The run and the mutations — ``docs/reports/TESTING_account-data-export.md``.
The expectations — literals from the fact, not through the code under test.
"""

from __future__ import annotations

import asyncio
import json
import os
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from src.api import deps, security
from src.api.cabinet import routes_account
from src.api.config import CabinetSettings
from src.api.main import app

TEST_URL = os.getenv("TEST_RLS_DATABASE_URL", "")
SUPER_URL = os.getenv("TEST_SUPERUSER_DATABASE_URL", TEST_URL)

pytestmark = pytest.mark.skipif(
    not TEST_URL, reason="TEST_RLS_DATABASE_URL не задан — выгрузка пропускается"
)

SECRET = "t" * 32
URL = "/cabinet/account/export"

# Literals of “foreign” and “secret” values: must be NOT found / found.
A_PROFILE, A_NOTE, A_TG = "Profil Alfa", "beleška Alfa", "111000111"
B_PROFILE, B_NOTE, B_TG = "Profil Beta", "beleška Beta", "222000222"
B_EMAIL = "beta-user@example.net"
TOKEN_HASH_A = "ab" * 32  # 64 hex, as sha256
ABUSE_HASH_A = "cd" * 32
ABUSE_LAST4_A = "q9z7"
# 3 tokens of B (other purposes): there must be none of them in the export of A
B_TOKENS = (
    ("email_verify", "1" * 64),
    ("email_verify", "2" * 64),
    ("channel_bind", "3" * 64),
)
OPERATOR_NOTE = "vnutrasnja-beleska-operatera"


async def _seed_one(c, tag, email, tg, profile, note, *, secrets: bool) -> dict:
    tid, uid = str(uuid.uuid4()), str(uuid.uuid4())
    await c.execute(
        text(
            "INSERT INTO tenants (id, country_code, tier, status, display_name) "
            "VALUES (CAST(:t AS uuid), 'RS', 'lite', 'trial', :n)"
        ),
        {"t": tid, "n": f"Firma {tag}"},
    )
    await c.execute(
        text(
            "INSERT INTO users (id, tenant_id, email, locale, role) "
            "VALUES (CAST(:u AS uuid), CAST(:t AS uuid), :e, 'ru', 'admin')"
        ),
        {"u": uid, "t": tid, "e": email},
    )
    await c.execute(
        text(
            "INSERT INTO user_channels (user_id, channel, external_id, status, linked_at) "
            "VALUES (CAST(:u AS uuid), 'telegram', :tg, 'active', now())"
        ),
        {"u": uid, "tg": tg},
    )
    await c.execute(
        text(
            "INSERT INTO profiles (tenant_id, name, is_active) "
            "VALUES (CAST(:t AS uuid), :n, true)"
        ),
        {"t": tid, "n": profile},
    )
    tender = (
        await c.execute(
            text("INSERT INTO tenders_normalized DEFAULT VALUES RETURNING id")
        )
    ).scalar_one()
    await c.execute(
        text(
            "INSERT INTO tender_notes (tenant_id, tender_id, body) "
            "VALUES (CAST(:t AS uuid), :td, :b)"
        ),
        {"t": tid, "td": tender, "b": note},
    )
    await c.execute(
        text(
            "INSERT INTO trial_requests (tenant_id, status, comment, decision_comment) "
            "VALUES (CAST(:t AS uuid), 'approved', :c, :d)"
        ),
        {"t": tid, "c": f"zahtev {tag}", "d": OPERATOR_NOTE},
    )
    await c.execute(
        text(
            "INSERT INTO ai_usage (tenant_id, period, level, count, tokens_total, "
            "cost_usd_total) VALUES (CAST(:t AS uuid), '2026-10', 'L1', 7, 1234, 0.5)"
        ),
        {"t": tid},
    )
    if secrets:
        await c.execute(
            text(
                "INSERT INTO auth_tokens (user_id, purpose, token_hash, expires_at) "
                "VALUES (CAST(:u AS uuid), 'magic_link', :h, now() + interval '1 hour')"
            ),
            {"u": uid, "h": TOKEN_HASH_A},
        )
        await c.execute(
            text(
                "INSERT INTO anti_abuse_identifiers (tenant_id, identifier_type, "
                "value_hash, value_last4, expires_at) VALUES (CAST(:t AS uuid), "
                "'email_domain', :h, :l4, now() + interval '1 year')"
            ),
            {"t": tid, "h": ABUSE_HASH_A, "l4": ABUSE_LAST4_A},
        )
    else:
        # auth_tokens without RLS: the tokens of B must not leak into the export of A
        for purpose, h in B_TOKENS:
            await c.execute(
                text(
                    "INSERT INTO auth_tokens (user_id, purpose, token_hash, expires_at) "
                    "VALUES (CAST(:u AS uuid), :p, :h, now() + interval '1 hour')"
                ),
                {"u": uid, "p": purpose, "h": h},
            )
    return {"tid": tid, "uid": uid, "tender": tender}


def _seed() -> tuple[dict, dict]:
    async def go():
        su = create_async_engine(SUPER_URL, poolclass=NullPool, future=True)
        async with su.begin() as c:
            a = await _seed_one(
                c, "A", "alfa-user@example.com", A_TG, A_PROFILE, A_NOTE, secrets=True
            )
            b = await _seed_one(c, "B", B_EMAIL, B_TG, B_PROFILE, B_NOTE, secrets=False)
        await su.dispose()
        return a, b

    return asyncio.run(go())


def _cleanup(*worlds: dict) -> None:
    async def go():
        su = create_async_engine(SUPER_URL, poolclass=NullPool, future=True)
        async with su.begin() as c:
            for w in worlds:
                t = {"t": w["tid"], "u": w["uid"]}
                await c.execute(
                    text("DELETE FROM auth_tokens WHERE user_id = CAST(:u AS uuid)"), t
                )
                await c.execute(
                    text("DELETE FROM user_channels WHERE user_id = CAST(:u AS uuid)"),
                    t,
                )
                for tbl in (
                    "tender_notes",
                    "profiles",
                    "trial_requests",
                    "ai_usage",
                    "anti_abuse_identifiers",
                    "users",
                ):
                    await c.execute(
                        text(f"DELETE FROM {tbl} WHERE tenant_id = CAST(:t AS uuid)"), t
                    )
                await c.execute(
                    text("DELETE FROM tenants WHERE id = CAST(:t AS uuid)"), t
                )
                await c.execute(
                    text("DELETE FROM tenders_normalized WHERE id = :td"),
                    {"td": w["tender"]},
                )
        await su.dispose()

    asyncio.run(go())


@pytest.fixture
def worlds():
    a, b = _seed()
    yield a, b
    _cleanup(a, b)


@pytest.fixture
def client():
    routes_account._EXPORT_PER_USER.reset()
    eng = create_async_engine(TEST_URL, poolclass=NullPool, future=True)
    maker = async_sessionmaker(eng, expire_on_commit=False)
    settings = CabinetSettings(
        CABINET_BASE_URL="https://nab.rs", CABINET_SECRET_KEY=SECRET
    )
    app.dependency_overrides[deps.get_session_factory] = lambda: maker
    app.dependency_overrides[deps.get_settings] = lambda: settings
    yield TestClient(app, follow_redirects=False, raise_server_exceptions=False)
    app.dependency_overrides.clear()
    routes_account._EXPORT_PER_USER.reset()


def _post(client, w: dict, csrf: bool = True):
    token = security.sign_session(w["uid"], w["tid"], SECRET, locale="ru")
    session = security.verify_session(token, SECRET)
    form = {"csrf": security.csrf_token(session, SECRET)} if csrf else {}
    return client.post(URL, data=form, cookies={deps.SESSION_COOKIE: token})


def test_export_has_own_data_and_attachment_headers(client, worlds):
    a, _ = worlds
    resp = _post(client, a)
    assert resp.status_code == 200, resp.headers.get("location")
    disp = resp.headers["content-disposition"]
    assert disp.startswith('attachment; filename="tendersrb-my-data-20')
    assert disp.endswith('.json"')
    assert resp.headers["content-type"].startswith("application/json")
    data = json.loads(resp.content)
    assert data["account"]["email"] == "alfa-user@example.com"
    assert data["tenant"]["display_name"] == "Firma A"
    assert [c["external_id"] for c in data["channels"]] == [A_TG]
    assert [p["name"] for p in data["profiles"]] == [A_PROFILE]
    assert [n["body"] for n in data["notes"]] == [A_NOTE]
    assert [t["comment"] for t in data["trial_requests"]] == ["zahtev A"]
    assert data["ai_usage"] == [
        {"period": "2026-10", "level": "L1", "count": 7, "tokens_total": 1234}
    ]
    # the fact of the presence of service data — without the values
    st = data["stored_but_not_exported"]
    assert [x["identifier_type"] for x in st["anti_abuse_identifiers"]] == [
        "email_domain"
    ]
    assert st["login_tokens_by_purpose"] == [{"purpose": "magic_link", "count": 1}]


def test_export_token_count_is_own_only(client, worlds):
    """auth_tokens without RLS: A sees 1 of its own token, not 1 + 3 tokens of B."""
    a, b = worlds
    data_a = json.loads(_post(client, a).content)
    assert data_a["stored_but_not_exported"]["login_tokens_by_purpose"] == [
        {"purpose": "magic_link", "count": 1}
    ]
    assert (
        sum(
            x["count"]
            for x in data_a["stored_but_not_exported"]["login_tokens_by_purpose"]
        )
        == 1
    )
    data_b = json.loads(_post(client, b).content)
    assert data_b["stored_but_not_exported"]["login_tokens_by_purpose"] == [
        {"purpose": "channel_bind", "count": 1},
        {"purpose": "email_verify", "count": 2},
    ]


def test_export_has_no_other_tenant_data(client, worlds):
    a, b = worlds
    body = _post(client, a).text
    for foreign in (
        B_PROFILE,
        B_NOTE,
        B_TG,
        B_EMAIL,
        "Firma B",
        "zahtev B",
        b["tid"],
        b["uid"],
    ):
        assert foreign not in body, foreign
    # and symmetrically: B sees its own and does not see A’s
    body_b = _post(client, b).text
    assert B_PROFILE in body_b and A_PROFILE not in body_b
    assert A_NOTE not in body_b and A_TG not in body_b


def test_export_has_no_secrets(client, worlds):
    a, _ = worlds
    body = _post(client, a).text
    for secret in (TOKEN_HASH_A, ABUSE_HASH_A, ABUSE_LAST4_A, OPERATOR_NOTE):
        assert secret not in body, secret
    for key in (
        "token_hash",
        "value_hash",
        "value_last4",
        "decision_comment",
        "cost_usd",
    ):
        assert key not in body, key


def test_anonymous_and_bad_csrf_are_refused(client, worlds):
    a, _ = worlds
    anon = client.post(URL, data={"csrf": "x"})
    assert anon.status_code == 303 and anon.headers["location"].startswith("/login")
    assert "attachment" not in anon.headers.get("content-disposition", "")
    bad = _post(client, a, csrf=False)
    assert bad.status_code == 303
    assert bad.headers["location"] == "/cabinet/account?exp_err=csrf#acct-export"
    # A GET on the export route does not exist (a read-only prefetch returns nothing)
    assert client.get(URL).status_code == 405


def test_rate_limit_per_user(client, worlds):
    a, b = worlds
    for _ in range(5):
        assert _post(client, a).status_code == 200
    limited = _post(client, a)
    assert limited.status_code == 303
    assert limited.headers["location"] == "/cabinet/account?exp_err=rate#acct-export"
    # the key is the user: another user is not blocked
    assert _post(client, b).status_code == 200
