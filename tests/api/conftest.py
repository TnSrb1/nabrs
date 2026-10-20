"""Shared fixtures/fakes for the cabinet tests (ADR-013: no live external APIs).

FakeSession imitates AsyncSession: it captures ORM adds and raw SQL, and returns
preset select results from a queue. Statements of the form
``set_config(...)`` (RLS) do NOT consume the queue — so tests can check
that ``app.current_tenant_id`` was set without disturbing the order of
select results.
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy.exc import IntegrityError

from src.api import anti_abuse as aa
from src.api.config import CabinetSettings
from src.api.emailer import MockEmailSender
from src.api.public import geo_cache

# --------------------------------------------------------------------------- #
# Fake DB layer
# --------------------------------------------------------------------------- #


class FakeResult:
    """Imitation of Result/ScalarResult from SQLAlchemy."""

    def __init__(self, *, items=None, scalar=None):
        self._items = list(items) if items is not None else []
        self._scalar = scalar

    def scalar_one(self):
        return self._scalar

    def scalar_one_or_none(self):
        return self._items[0] if self._items else None

    def scalars(self):
        return self

    def first(self):
        return self._items[0] if self._items else None

    def all(self):
        return list(self._items)

    def fetchall(self):
        # preview_service reads its result via fetchall() — the fake must support it.
        return list(self._items)

    def __iter__(self):
        # A real Result is iterated over rows (as, for example,
        # matching.channel_states reads it) — the fake must support the same.
        return iter(self._items)


class FakeSession:
    """Imitation of AsyncSession for unit tests of services."""

    def __init__(
        self,
        *,
        results=None,
        get_map=None,
        flush_raises_for=None,
        user_locale=None,
        access_until=None,
        user_deleted_at=None,
        access_row=True,
        whoami_email=None,
        whoami_tenant_name=None,
        whoami_row=True,
        has_pending_request=False,
        tenant_tier="lite",
        tenant_status="active",
        enabled_sources=None,
        answers_tenant_tier=False,
    ):
        self.added: list = []
        # Registry of enabled sources (``src.domain.sources``, SRC-0a) — like
        # the locale/access gate, with its own SELECT that does not consume the results queue.
        # By default — prod fact of 30.09.2026: exactly jnportal (RS) is enabled.
        self._enabled_sources = (
            [
                SimpleNamespace(
                    source_key="jnportal",
                    display_name="Portal javnih nabavki (UJN)",
                    country_codes=["RS"],
                    available_for_tiers=None,
                    # prod fact: jnportal, polling_interval_sec = 1800 (30 min)
                    polling_interval_sec=1800,
                )
            ]
            if enabled_sources is None
            else list(enabled_sources)
        )
        self.executed: list[tuple[str, dict | None]] = []
        self.committed = 0
        self.rolled_back = 0
        self._results = list(results or [])
        self._get_map = get_map or {}
        # name of the class on whose add+flush an IntegrityError must be raised
        self._flush_raises_for = flush_raises_for
        self._last_added_type: str | None = None
        # users.locale, returned to the cabinet language resolver (deps.resolve_cabinet_locale).
        self._user_locale = user_locale
        # Answer to the access gate (deps.require_active_access): by default access
        # is LIVE and unlimited (access_until IS NULL) — otherwise every cabinet
        # test would get 303 to /cabinet/subscription. Freeze/revocation the test
        # sets explicitly: access_until=<past> / user_deleted_at=<date>;
        # access_row=False = no row at all (fail-closed).
        self._access_until = access_until
        self._user_deleted_at = user_deleted_at
        self._access_row = access_row
        # Session identification in the header (deps.resolve_cabinet_whoami) — like
        # the locale/access gate, with its own SELECT that does not touch the results queue.
        self._whoami_email = whoami_email
        self._whoami_tenant_name = whoami_tenant_name
        self._whoami_row = whoami_row
        self._has_pending_request = has_pending_request
        # tenant tier/status for the menu (deps.resolve_cabinet_whoami → nav_access).
        self._tenant_tier = tenant_tier
        self._tenant_status = tenant_status
        self._answers_tenant_tier = answers_tenant_tier

    async def execute(self, statement, params=None):
        text = str(statement)
        self.executed.append((text, params))
        if "set_config" in text or "app.current_tenant_id" in text:
            return FakeResult()  # RLS call — do not touch the queue
        # COUNT of "upcoming deadlines" (cockpit tile): scalar 0 by default, the queue
        # results are not touched — otherwise it would shift the answers prepared for the route.
        if "count(DISTINCT tn.id)" in text:
            return FakeResult(scalar=0)
        # The cabinet language resolver hangs on every cabinet route (context-processor
        # of the chrome) and makes ONE select of locale BEFORE the route’s own queries. Like
        # set_config, it must not consume the shared results queue — otherwise it would
        # shift the answers prepared for the route. The locale is set via a separate
        # user_locale field (default None → language default).
        if "SELECT locale FROM users" in text:
            return FakeResult(items=[SimpleNamespace(locale=self._user_locale)])
        # The access gate hangs on content routers and, like the locale resolver,
        # asks ITS OWN question — it does not consume the results queue.
        if "SELECT t.access_until" in text:
            row = SimpleNamespace(
                access_until=self._access_until, deleted_at=self._user_deleted_at
            )
            return FakeResult(items=[row] if self._access_row else [])
        if "whoami_email" in text:
            row = SimpleNamespace(
                whoami_email=self._whoami_email,
                whoami_tenant_name=self._whoami_tenant_name,
                whoami_access_until=self._access_until,
                whoami_has_pending=self._has_pending_request,
                whoami_tier=self._tenant_tier,
                whoami_status=self._tenant_status,
            )
            return FakeResult(items=[row] if self._whoami_row else [])
        # Tenant tier/status for the hub’s subscription section (subscription_service.get_tenant_tier).
        # Only on an explicit flag: the other tests put this row into the results queue.
        if self._answers_tenant_tier and "SELECT tier, status FROM tenants" in text:
            row = SimpleNamespace(tier=self._tenant_tier, status=self._tenant_status)
            return FakeResult(items=[row])
        if "SELECT polling_interval_sec FROM sources" in text:
            return FakeResult(
                items=[
                    SimpleNamespace(
                        polling_interval_sec=getattr(r, "polling_interval_sec", None)
                    )
                    for r in self._enabled_sources
                ]
            )
        if "FROM sources" in text and "enabled = true" in text:
            return FakeResult(items=list(self._enabled_sources))
        if self._results:
            return self._results.pop(0)
        return FakeResult()

    def add(self, obj):
        self.added.append(obj)
        self._last_added_type = type(obj).__name__

    async def flush(self):
        if self._flush_raises_for and self._last_added_type == self._flush_raises_for:
            raise IntegrityError("dup", {}, Exception("unique_violation"))

    async def commit(self):
        self.committed += 1

    async def rollback(self):
        self.rolled_back += 1

    async def get(self, model, pk):
        return self._get_map.get(
            type(model).__name__ if not isinstance(model, type) else model.__name__
        )

    # async context manager (session factory protocol)
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


def make_session_factory(session: FakeSession):
    """Returns a callable that gives the passed session back as an async CM."""

    def factory():
        return session

    return factory


@pytest.fixture(autouse=True)
def _empty_geo_cache():
    """GEO-2: ``/tenderi`` aggregates are cached in-process for the whole process
    (TTL 1h, :mod:`src.api.public.geo_cache`) — without a reset the tests share the cache
    between themselves, and tests that do not touch GEO sections (the tender card also
    calls the cache for cross-linking, TASK geo_landings step 5) would catch an extra
    ``session.execute`` and a desync of the ``FakeResult`` queue. An empty
    pre-fill = not a single extra query by default; a test that needs real sections
    overwrites the cache itself after the fixture.
    """
    geo_cache.clear_cache()
    future = time.monotonic() + 3600
    geo_cache._cpv_cache = (future, {})
    geo_cache._region_cache = (future, {})
    geo_cache._cpv_dir_cache = (future, {"groups": {}, "codes": {}})
    # The “Source” filter of the portal: by default — prod fact of 30.09.2026 (one
    # enabled jnportal); without a pre-fill the source cache would make an extra
    # SELECT in the queue of the section’s tests.
    geo_cache._sources_cache = (future, {"jnportal": "Portal javnih nabavki (UJN)"})
    yield
    geo_cache.clear_cache()


# --------------------------------------------------------------------------- #
# Settings / collaborators
# --------------------------------------------------------------------------- #


@pytest.fixture
def settings() -> CabinetSettings:
    return CabinetSettings(
        CABINET_BASE_URL="https://nab.rs",
        CABINET_SECRET_KEY="test-secret",
        CABINET_COOKIE_SECURE=False,
        TURNSTILE_SITE_KEY="site-key",
    )


@pytest.fixture
def email_sender() -> MockEmailSender:
    return MockEmailSender()


def ok_turnstile() -> aa.TurnstileVerifier:
    return aa.NoopTurnstileVerifier(True)


def bad_turnstile() -> aa.TurnstileVerifier:
    return aa.NoopTurnstileVerifier(False)


def good_email() -> aa.EmailValidator:
    return aa.NoopEmailValidator(deliverable=True, disposable=False, risky=False)


def disposable_email() -> aa.EmailValidator:
    return aa.NoopEmailValidator(deliverable=False, disposable=True, risky=False)


def risky_email() -> aa.EmailValidator:
    return aa.NoopEmailValidator(deliverable=True, disposable=False, risky=True)


def unknown_email() -> aa.EmailValidator:
    """Gmail / large provider: result=unknown (risky, NOT undeliverable)."""
    return aa.NoopEmailValidator(
        deliverable=False,
        disposable=False,
        risky=True,
        reason="unknown",
        undeliverable=False,
    )


def undeliverable_email() -> aa.EmailValidator:
    """Explicitly undeliverable: result=undeliverable → block."""
    return aa.NoopEmailValidator(
        deliverable=False,
        disposable=False,
        risky=False,
        reason="undeliverable",
        undeliverable=True,
    )


def no_api_key_email() -> aa.EmailValidator:
    """No KICKBOX_API_KEY — fail-open (risky, NOT undeliverable)."""
    return aa.NoopEmailValidator(
        deliverable=False,
        disposable=False,
        risky=True,
        reason="no_api_key",
        undeliverable=False,
    )


# --------------------------------------------------------------------------- #
# Lightweight stand-ins for ORM rows returned from selects/get
# --------------------------------------------------------------------------- #


def make_user(**kw):
    base = dict(
        id="00000000-0000-0000-0000-0000000000aa",
        tenant_id="00000000-0000-0000-0000-0000000000bb",
        email="user@company.rs",
        email_verified_at=datetime.now(timezone.utc),
        deleted_at=None,
        locale="ru",  # cabinet interface language: travels in the signed session at login
    )
    base.update(kw)
    return SimpleNamespace(**base)


def make_tenant(**kw):
    base = dict(
        id="00000000-0000-0000-0000-0000000000bb",
        status="trial",
        tier=None,
    )
    base.update(kw)
    return SimpleNamespace(**base)


def make_token(*, purpose, consumed_at=None, expired=False, user_id="u"):
    expires = datetime.now(timezone.utc) + (
        timedelta(hours=-1) if expired else timedelta(hours=1)
    )
    return SimpleNamespace(
        purpose=purpose,
        consumed_at=consumed_at,
        expires_at=expires,
        user_id=user_id,
    )


@pytest.fixture(autouse=True)
def _reset_source_caches():
    """Source-name caches (TTL, in-process) must not leak between tests."""
    from src.api.public import sources_page, sources_view
    from src.domain import sources

    sources.reset_source_labels_cache()
    sources_view.clear_cache()
    sources_page.clear_cache()
    yield
    sources.reset_source_labels_cache()
    sources_view.clear_cache()
    sources_page.clear_cache()


@pytest.fixture
def freeze_feed_clock(monkeypatch):
    """Freezing “now” for “days until deadline” (``templating._days_left``).

    ``freeze(now_utc)`` → ``deadline_in(days)``: a deadline at 23:00 Belgrade time
    ``days`` CALENDAR days after the frozen moment — exactly what the product computes. The
    fixture builds the deadline by the calendar, not ``now + N*24h``: “+8×24h”
    across the switch to winter time (25.10.2026) gives 7 calendar days, and the “8 days”
    boundary drifted by the hour of the day (see docs/reports/TESTING_feed-boundary-clock.md).
    """
    from datetime import time as _time
    from zoneinfo import ZoneInfo

    from src.api import templating
    from src.domain.tz import to_local

    belgrade = ZoneInfo("Europe/Belgrade")

    class _Frozen(datetime):
        now_utc: datetime

        @classmethod
        def now(cls, tz=None):  # noqa: D102
            return cls.now_utc if tz is not None else cls.now_utc.replace(tzinfo=None)

    def freeze(now_utc: datetime):
        _Frozen.now_utc = now_utc
        monkeypatch.setattr(templating, "datetime", _Frozen)

        def deadline_in(days: int) -> datetime:
            day = to_local(now_utc).date() + timedelta(days=days)
            return datetime.combine(day, _time(23), tzinfo=belgrade).astimezone(
                timezone.utc
            )

        return deadline_in

    return freeze
