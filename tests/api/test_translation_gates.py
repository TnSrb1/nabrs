"""The paid gate of the card translation: freeze / plan / limit / accounting (VG-SPEND).

Before this task the translation (``src/ai/translation.py``) was the ONLY AI action outside the accounting contour: no plan, no freeze, no limit, no row in ``ai_usage``. Registration on the site is self-service, the trial = Pro rights, so anyone who registered could burn Anthropic by the “Translate” button without limit, and a frozen tenant — for free and after the end of access.

Here the ROUTE is checked as a whole (both fragments: the heading and the description) with the real translation module: only the DB (``_GateSession``), Redis (``FakeRedis``) and the AI client itself (``_FakeAi``) are mocked — there is no network (ADR-013). The key assertion of each test — **was the model client called**: the gate must cut off BEFORE the money, not after.

The logic of the translation itself (chunking/cache/degradation) is not duplicated — tests/ai/test_translation.py.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from src.ai import translation
from src.ai.usage import _TIER_LIMITS
from src.api import deps
from src.api.cabinet import routes_tender_ai
from src.api.cabinet.tender_ai_service import TenderAnalyses
from src.api.main import app
from tests.api.conftest import FakeResult, FakeSession

SESS = {"uid": "u-1", "tid": "11111111-1111-1111-1111-111111111111"}
_TENDER = "22222222-2222-2222-2222-222222222222"
_PROFILE = "33333333-3333-3333-3333-333333333333"

_TITLE_SR = "Набавка рачунара"
_DESC_SR = "Предмет јавне набавке је испорука рачунарске опреме."
_TITLE_RU = "Закупка компьютеров"
_DESC_RU = "Предмет закупки — поставка компьютерного оборудования."

_DESC_FRAG = f"/app/tenders/{_TENDER}/description?profile_id={_PROFILE}"
_TITLE_FRAG = f"/app/tenders/{_TENDER}/title?profile_id={_PROFILE}"

# The mark “the original is shown” in both fragments for a NON-monetary reason
# (model failure/timeout): the keys ct_unavailable / td_desc_unavailable.
_UNAVAILABLE_RU = "недоступ"
# ‼️ A monetary refusal must be called by ITS OWN NAME. Until 15.08.2026 an exhausted
# limit was rendered with the same phrase “translation unavailable” as a model failure:
# the client read the refusal about the money as a temporary glitch and pressed the button again
# (checked by the render of the fragment, not by reading the code). The owner’s requirement — "nothing
# must be switched off silently, the subscriber must see that they have exhausted the limit".
_EXHAUSTED_RU = "лимит перевод"

# The limit is taken FROM the SSOT (_TIER_LIMITS), not as a number: the test is about the behavior of the gate at
# the boundary, not about a concrete figure — an edit of the plan must not break it.
_PRO_TRANSLATE_LIMIT = _TIER_LIMITS["pro"]["translate"]


# --------------------------------------------------------------------------- #
#  Fakes: the gate DB, Redis, the AI client
# --------------------------------------------------------------------------- #


class _GateSession(FakeSession):
    """A session that answers exactly three gate queries, and catches the write of the usage.

    A dispatcher by the SQL text, not a queue of results: the order of the queries inside the route is an implementation detail, and the test is about the meaning (which plan / how much was spent). ``usage_writes`` — the parameters of the UPSERT of ``ai_usage`` (the accounting), ``used`` grows by ``calls``, as the UPSERT itself does in the DB.
    """

    def __init__(self, *, tier="pro", status="active", access_until=None, used=0):
        super().__init__()
        self.tier = tier
        self.status = status
        self.access_until = access_until
        self.used = used
        self.usage_writes: list[dict] = []

    async def execute(self, statement, params=None):
        sql = str(statement)
        # whoami (deps.resolve_cabinet_whoami, hangs on _CABINET_DEPS by the same
        # router) also joins tenants — it is not the access gate nor the UPSERT
        # of usage, we delegate to the parent BEFORE the common substring dispatcher below.
        if "whoami_email" in sql:
            return await super().execute(statement, params)
        if not any(k in sql for k in ("tenants", "ai_usage")):
            return await super().execute(statement, params)  # it also logs
        self.executed.append((sql, params))  # the order matters for the RLS check
        if "FROM tenants" in sql:
            return FakeResult(
                items=[
                    SimpleNamespace(
                        tier=self.tier,
                        status=self.status,
                        access_until=self.access_until,
                    )
                ]
            )
        if "FROM ai_usage" in sql:
            return FakeResult(items=[SimpleNamespace(count=self.used)])
        self.usage_writes.append(dict(params or {}))  # only the UPSERT remained
        self.used += int((params or {}).get("calls", 0))
        return FakeResult()


class FakeRedis:
    """In-memory Redis: only get/set with ex, as translation calls them."""

    def __init__(self) -> None:
        self.store: dict[str, str] = {}

    async def get(self, key):
        return self.store.get(key)

    async def set(self, key, value, ex=None):
        self.store[key] = value


class _FakeAi:
    """The AI client: counts the calls. ‼️ An empty ``calls`` = no money was spent."""

    def __init__(self) -> None:
        self.calls: list = []

    async def analyze(self, request):
        self.calls.append(request)
        return SimpleNamespace(
            output={"title": _TITLE_RU, "description": _DESC_RU},
            model="claude-haiku-4-5-20251001",
            tokens_input=500,
            tokens_output=400,
            tokens_cached=0,
            cost_usd=0.0025,
        )


class _TrackedSession:
    """An async CM for one call of ``session_factory()`` — counts the opening/closing."""

    def __init__(self, factory: "_TrackingFactory") -> None:
        self._factory = factory

    async def __aenter__(self):
        self._factory.active += 1
        return self._factory.session

    async def __aexit__(self, *exc):
        self._factory.active -= 1
        return False


class _TrackingFactory:
    """The session_factory double: counts the ACTIVE and the total of created sessions.

    ``session`` — the same DB double (``_GateSession``) for all the “sessions” of the request, so that the accounting (``used``/``usage_writes``/``executed``) accumulates across session #1 (the gate) and session #2 (the charge), as it would accumulate in one real DB transaction over the pool. We measure by the counter of the context manager itself (symmetric to ``__aenter__``/``__aexit__``), not by the structure of the route’s code — otherwise the test would prove “the code is written so”, not “the session at the moment of the model call is actually closed”.
    """

    def __init__(self, session) -> None:
        self.session = session
        self.active = 0
        self.created = 0

    def __call__(self) -> _TrackedSession:
        self.created += 1
        return _TrackedSession(self)


class _FakeAiTrackingSessions(_FakeAi):
    """The AI client, recording the number of ACTIVE DB sessions at the moment of its call."""

    def __init__(self, factory: _TrackingFactory) -> None:
        super().__init__()
        self._factory = factory
        self.active_at_call: list[int] = []

    async def analyze(self, request):
        self.active_at_call.append(self._factory.active)
        return await super().analyze(request)


def _data():
    return TenderAnalyses(
        tender_id=_TENDER,
        profile_id=_PROFILE,
        profile_name="ИТ набавке",
        title=_TITLE_SR,
        customer_name="Општина Нови Сад",
        deadline_at=datetime(2026, 3, 15, 12, 0, tzinfo=timezone.utc),
        source_url="https://portal.rs/t/1",
        outputs={},
        description=_DESC_SR,
    )


def _async(return_value):
    async def _stub(*args, **kwargs):
        return return_value

    return _stub


def _setup(monkeypatch, session, *, redis=None):
    """The route with the real translation: only the DB, Redis and the AI client are substituted."""
    monkeypatch.setattr(routes_tender_ai.svc, "get_tender_analyses", _async(_data()))
    ai = _FakeAi()
    monkeypatch.setattr(translation.ai_client, "AiClient", lambda *a, **kw: ai)
    monkeypatch.setattr(
        translation, "_DEFAULT_AI_CLIENT", None, raising=False
    )  # singleton — per test

    app.dependency_overrides[deps.get_session_factory] = lambda: (lambda: session)
    app.dependency_overrides[deps.get_optional_session] = lambda: SESS
    app.dependency_overrides[deps.get_redis] = lambda: (
        redis if redis is not None else FakeRedis()
    )
    return ai, TestClient(app, follow_redirects=False)


def _setup_tracking(monkeypatch, session, *, redis=None):
    """As ``_setup``, but the ``session_factory`` is a counter of active/created sessions
    (VG-SPEND: a DB session must not live during the model call, the project rules)."""
    monkeypatch.setattr(routes_tender_ai.svc, "get_tender_analyses", _async(_data()))
    factory = _TrackingFactory(session)
    ai = _FakeAiTrackingSessions(factory)
    monkeypatch.setattr(translation.ai_client, "AiClient", lambda *a, **kw: ai)
    monkeypatch.setattr(
        translation, "_DEFAULT_AI_CLIENT", None, raising=False
    )  # singleton — per test

    app.dependency_overrides[deps.get_session_factory] = lambda: factory
    app.dependency_overrides[deps.get_optional_session] = lambda: SESS
    app.dependency_overrides[deps.get_redis] = lambda: (
        redis if redis is not None else FakeRedis()
    )
    return ai, factory, TestClient(app, follow_redirects=False)


def teardown_function():
    app.dependency_overrides.clear()


def _past():
    return datetime.now(timezone.utc) - timedelta(days=1)


# --------------------------------------------------------------------------- #
#  1. Freeze: access expired → do not call the model
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("frag", [_DESC_FRAG, _TITLE_FRAG])
def test_frozen_tenant_never_calls_the_model(monkeypatch, frag):
    """‼️ The core of the task: ``access_until`` expired → the translation is not bought.

    A frozen tenant is cut off from matching and all the AI buttons, and since #469 — from the content sections of the cabinet entirely: ``deps.require_active_access`` takes them to the subscription page EVEN BEFORE the body of the fragment. Therefore the expectation here is a redirect (303 for an ordinary request, HTMX gets 401 + ``HX-Redirect``), not a 200 with the original, as it was before the common gate appeared.

    ‼️ The invariant of the test has NOT changed and is checked by the same two assertions: the model is not called and there is no charge. The translation gate (``_translation_allowed``) stays as the second line — it holds the same axes for the cases when the common gate is not applied (for example, the plan has exhausted the limit with live access, see the test below).
    """
    session = _GateSession(status="trial", tier=None, access_until=_past())
    ai, client = _setup(monkeypatch, session)

    resp = client.get(f"{frag}&lang=ru")

    # owner 10.10: the frozen cabinet is open (the common gate does not take it away), but the translation gate
    # holds the freeze axis itself — the original instead of the translation, the model was not called.
    assert (
        resp.status_code == 200
    ), "The cabinet is open to the frozen one (owner 10.10)"
    assert ai.calls == [], "a frozen tenant has no right to call the model"
    assert session.usage_writes == [], "nothing to charge — there was no call"


# --------------------------------------------------------------------------- #
#  2. The monthly limit is exhausted → do not call the model
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("frag", [_DESC_FRAG, _TITLE_FRAG])
def test_exhausted_limit_never_calls_the_model(monkeypatch, frag):
    """‼️ trial = Pro rights (400 translations/month): on the 400th counter the gate is closed.

    The plan is taken as trial+NULL — exactly what self-service has on production.
    """
    session = _GateSession(status="trial", tier=None, used=_PRO_TRANSLATE_LIMIT)
    ai, client = _setup(monkeypatch, session)

    resp = client.get(f"{frag}&lang=ru")

    assert resp.status_code == 200
    assert ai.calls == [], "the limit is exhausted — we do not call the model"
    assert session.usage_writes == []
    assert (
        _EXHAUSTED_RU in resp.text.lower()
    ), "the reason of the refusal is named, not a “glitch”"


@pytest.mark.parametrize("frag", [_DESC_FRAG, _TITLE_FRAG])
def test_last_call_under_limit_still_translates(monkeypatch, frag):
    """The boundary is inclusive from the OTHER side: the last call still translates."""
    session = _GateSession(status="trial", tier=None, used=_PRO_TRANSLATE_LIMIT - 1)
    ai, client = _setup(monkeypatch, session)

    resp = client.get(f"{frag}&lang=ru")

    assert resp.status_code == 200
    assert len(ai.calls) >= 1


# --------------------------------------------------------------------------- #
#  3. A successful translation → the usage is recorded in ai_usage
# --------------------------------------------------------------------------- #


def test_successful_description_translation_records_usage(monkeypatch):
    """‼️ The translation goes into ``ai_usage`` at the level ``translate`` — otherwise the limit
    counts an empty counter, and the spending is not visible in the report."""
    session = _GateSession(tier="pro", used=0)
    ai, client = _setup(monkeypatch, session)

    resp = client.get(f"{_DESC_FRAG}&lang=ru")

    assert resp.status_code == 200
    assert _DESC_RU in resp.text
    assert len(ai.calls) == 1
    assert len(session.usage_writes) == 1
    write = session.usage_writes[0]
    assert write["level"] == "translate"
    assert write["calls"] == 1  # the limit unit = one press
    assert write["cost"] > 0  # cost_usd_total must grow
    assert write["tokens"] == 900  # tokens_input + tokens_output
    assert write["tenant_id"] == SESS["tid"]
    assert session.used == 1  # the counter of the level grew by exactly 1
    # ‼️ RLS: ai_usage is tenant-scoped → set_tenant BEFORE the write of the usage.
    stmts = [sql for sql, _ in session.executed]
    rls = next(i for i, s in enumerate(stmts) if "app.current_tenant_id" in s)
    upsert = next(i for i, s in enumerate(stmts) if "INSERT INTO ai_usage" in s)
    assert rls < upsert


def test_title_translation_charges_one_unit_not_per_chunk(monkeypatch):
    """The heading is also a paid call and the same level ``translate``."""
    session = _GateSession(tier="pro", used=0)
    ai, client = _setup(monkeypatch, session)

    resp = client.get(f"{_TITLE_FRAG}&lang=ru")

    assert resp.status_code == 200
    assert _TITLE_RU in resp.text
    assert len(ai.calls) == 1
    assert [w["level"] for w in session.usage_writes] == ["translate"]
    assert session.usage_writes[0]["calls"] == 1


def test_multi_chunk_description_charges_one_unit_but_full_cost(monkeypatch):
    """The limit unit is one USER action, the money — over all the chunks.

    Otherwise a long spec (5–6 chunks) would eat the monthly quota in a dozen openings, and a short one would lie about the cost.
    """
    session = _GateSession(tier="pro", used=0)
    ai, client = _setup(monkeypatch, session)
    # A description longer than one chunk (_CHUNK_MAX_CHARS = 2500) → several calls.
    long_desc = "\n\n".join(f"[p{i}]" + "я" * 1500 for i in range(4))
    data = TenderAnalyses(
        tender_id=_TENDER,
        profile_id=_PROFILE,
        profile_name="ИТ набавке",
        title=_TITLE_SR,
        customer_name=None,
        deadline_at=None,
        source_url=None,
        outputs={},
        description=long_desc,
    )
    monkeypatch.setattr(routes_tender_ai.svc, "get_tender_analyses", _async(data))

    resp = client.get(f"{_DESC_FRAG}&lang=ru")

    assert resp.status_code == 200
    assert len(ai.calls) > 1, "a long description must be split into chunks"
    assert len(session.usage_writes) == 1
    assert session.usage_writes[0]["calls"] == 1  # ONE limit unit
    # The money — the sum over all the chunks (0.0025 per call in the fake).
    assert session.usage_writes[0]["cost"] == pytest.approx(0.0025 * len(ai.calls))
    assert session.usage_writes[0]["tokens"] == 900 * len(ai.calls)


# --------------------------------------------------------------------------- #
#  4. A cache hit → no call and no charge
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("frag", [_DESC_FRAG, _TITLE_FRAG])
def test_cache_hit_costs_neither_call_nor_limit(monkeypatch, frag):
    """‼️ A repeated showing of an already translated card does not spend the quota.

    Otherwise a user who opened the same card twice would pay twice for the same text, and the monthly limit would burn out on flipping the pages.
    """
    redis = FakeRedis()
    session = _GateSession(tier="pro", used=0)
    ai, client = _setup(monkeypatch, session, redis=redis)

    first = client.get(f"{frag}&lang=ru")
    assert first.status_code == 200
    assert len(ai.calls) >= 1
    calls_after_first = len(ai.calls)
    writes_after_first = len(session.usage_writes)

    second = client.get(f"{frag}&lang=ru")

    assert second.status_code == 200
    assert (
        len(ai.calls) == calls_after_first
    ), "the second showing — from the cache, without the model"
    assert (
        len(session.usage_writes) == writes_after_first
    ), "a cache hit does not charge"
    assert session.used == 1  # the counter did not move since the first translation


# --------------------------------------------------------------------------- #
#  5. The contract “content as is”: lang=sr does not call the model at all
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("frag", [_DESC_FRAG, _TITLE_FRAG])
def test_original_lang_calls_neither_model_nor_gate(monkeypatch, frag):
    """The owner’s regression: the original is free — neither the model, nor a usage record."""
    session = _GateSession(tier="pro", used=0)
    ai, client = _setup(monkeypatch, session)

    resp = client.get(f"{frag}&lang=sr")

    assert resp.status_code == 200
    assert ai.calls == []
    assert session.usage_writes == []
    assert (
        _UNAVAILABLE_RU not in resp.text
    )  # this is not a refusal, but the regular original


@pytest.mark.parametrize("frag", [_DESC_FRAG, _TITLE_FRAG])
def test_default_render_calls_neither_model_nor_gate(monkeypatch, frag):
    """Without ?lang= — the same original (the default of the “as is” contract)."""
    session = _GateSession(tier="pro", used=0)
    ai, client = _setup(monkeypatch, session)

    resp = client.get(frag)

    assert resp.status_code == 200
    assert ai.calls == []
    assert session.usage_writes == []


# --------------------------------------------------------------------------- #
#  6. The connection pool: a DB session must not live during the model call
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("frag", [_DESC_FRAG, _TITLE_FRAG])
def test_model_called_with_no_open_db_session(monkeypatch, frag):
    """‼️ The core of the task (26.07): at the moment of the model call the session from ``session_factory``
    is closed — we prove it by the COUNTER of the active sessions of the factory double, not by the text
    of the code. The translation of the description holds the call up to 60 s, of the heading — up to 20 s: holding
    a pool connection all this time means putting the whole cabinet down on a mass click
    “Translate” (a small pool, a public test — the project rules).
    """
    session = _GateSession(tier="pro", used=0)
    ai, factory, client = _setup_tracking(monkeypatch, session)

    resp = client.get(f"{frag}&lang=ru")

    assert resp.status_code == 200
    assert ai.active_at_call, "the model must be called"
    assert all(
        active == 0 for active in ai.active_at_call
    ), "at the moment of the model call the DB session must be closed (active == 0)"
    assert factory.active == 0  # and after the request nothing is left open


def test_cache_hit_never_opens_charge_session(monkeypatch):
    """A cache hit: the gate is checked again (session #1), but there is nothing to charge —
    the second session (``_charge_translation``) is not opened at all.

    The absolute number of sessions per request cannot be compared (the chrome also takes its own session for the interface locale) — we compare the DELTA of created sessions between the request that really translated (the gate + the model + the charge) and the cache hit (the gate + the model-from-cache, without the charge): on the cache hit there must be exactly ONE session fewer.
    """
    redis = FakeRedis()
    session = _GateSession(tier="pro", used=0)
    ai, factory, client = _setup_tracking(monkeypatch, session, redis=redis)

    before_first = factory.created
    first = client.get(f"{_DESC_FRAG}&lang=ru")
    assert first.status_code == 200
    assert len(ai.calls) == 1
    sessions_for_real_translate = factory.created - before_first

    before_second = factory.created
    second = client.get(f"{_DESC_FRAG}&lang=ru")

    assert second.status_code == 200
    assert (
        len(ai.calls) == 1
    ), "the second showing — from the cache, the model was not called"
    sessions_for_cache_hit = factory.created - before_second
    # Exactly one session fewer — the very one that would have been opened for the charge.
    assert sessions_for_cache_hit == sessions_for_real_translate - 1
