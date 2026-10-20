"""Tests of the public card of the tender ``/tender/{id}`` through the FastAPI TestClient.

The DB and session are mocked via dependency_overrides; templates render for real.
web-ux-6: the page is public (sr/ru/en), with a conversion CTA and SEO/OG meta.
"""

from __future__ import annotations

import json
import re
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

from fastapi.testclient import TestClient
from sqlalchemy.exc import DataError

from src.api import deps, repo
from src.api.main import app
from tests.api.conftest import FakeResult, FakeSession


def _tender(**kw):
    base = dict(
        id="11111111-1111-4111-8111-111111111111",
        title="Видеонаблюдение для школы",
        customer_name="ОШ «Вук Караджич»",
        amount_eur=12345.0,
        is_amount_estimated=False,
        deadline_at=datetime.now(timezone.utc) + timedelta(days=20),
        status="active",
        regions=["RS-00"],
        procedure_type=None,
        description=None,
        classifications=None,
        source_url="https://jnportal.rs/t/1",
        canonical_tender_id=None,
    )
    base.update(kw)
    return SimpleNamespace(**base)


def _async_value(value):
    async def _stub(*args, **kwargs):
        return value

    return _stub


def _client(session: FakeSession, *, sess=None):
    def factory():
        return session

    app.dependency_overrides[deps.get_session_factory] = lambda: factory
    app.dependency_overrides[deps.get_optional_session] = lambda: sess
    return TestClient(app)


def teardown_function():
    app.dependency_overrides.clear()


def test_tender_page_present():
    session = FakeSession(results=[FakeResult(items=[_tender()])])
    client = _client(session)
    resp = client.get("/tender/11111111-1111-4111-8111-111111111111")
    assert resp.status_code == 200
    # sr: the tender data in Latin (owner 10.10.2026), a literal from the fixture
    assert "Videonabljudenie" in resp.text
    assert "Видеонаблюдение" not in resp.text
    # an anonymous user does not see the AI analysis (only the conversion CTA)
    assert "AI-analiza za vaš profil" not in resp.text


def test_tender_page_conversion_cta_for_anon():
    """The main block for growth: an anonymous user sees the CTA “Start for free” → /signup."""
    session = FakeSession(results=[FakeResult(items=[_tender()])])
    client = _client(session)
    resp = client.get("/tender/11111111-1111-4111-8111-111111111111?lang=ru")
    assert resp.status_code == 200
    assert "Начать бесплатно" in resp.text
    assert 'href="/signup"' in resp.text
    assert 'href="/plans"' in resp.text
    assert "Тарифы" in resp.text


def test_tender_page_seo_meta_and_og():
    """SEO: a meaningful title/description + OG tags from the tender data."""
    session = FakeSession(results=[FakeResult(items=[_tender()])])
    client = _client(session)
    resp = client.get("/tender/11111111-1111-4111-8111-111111111111?lang=sr")
    assert resp.status_code == 200
    # og:title = the tender’s heading
    assert 'property="og:title" content="Videonabljudenie dlja školy"' in resp.text
    # meta description contains the customer and the sr tail
    assert "OŠ «Vuk Karadžič»" in resp.text
    assert "javna nabavka u Srbiji" in resp.text


def test_tender_page_i18n_parity_labels():
    """The public page is multilingual: the ru/en chrome is present."""
    session = FakeSession(results=[FakeResult(items=[_tender()])])
    client = _client(session)
    resp_ru = client.get("/tender/11111111-1111-4111-8111-111111111111?lang=ru")
    assert "Срок подачи" in resp_ru.text
    teardown_function()

    session = FakeSession(results=[FakeResult(items=[_tender()])])
    client = _client(session)
    resp_en = client.get("/tender/11111111-1111-4111-8111-111111111111?lang=en")
    assert "Submission deadline" in resp_en.text
    assert "Start for free" in resp_en.text


def test_tender_page_urgency_badge():
    """A burning deadline (≤7 d.) → the urgency accent."""
    tender = _tender(deadline_at=datetime.now(timezone.utc) + timedelta(days=2))
    session = FakeSession(results=[FakeResult(items=[tender])])
    client = _client(session)
    resp = client.get("/tender/11111111-1111-4111-8111-111111111111?lang=ru")
    assert resp.status_code == 200
    assert "badge-urgent" in resp.text
    assert "Осталось 2 дня" in resp.text


def test_tender_page_expired_badge():
    tender = _tender(deadline_at=datetime.now(timezone.utc) - timedelta(days=3))
    session = FakeSession(results=[FakeResult(items=[tender])])
    client = _client(session)
    resp = client.get("/tender/11111111-1111-4111-8111-111111111111?lang=ru")
    assert resp.status_code == 200
    assert "badge-expired" in resp.text
    assert "Срок истёк" in resp.text


def test_tender_page_cpv_chip():
    tender = _tender(
        classifications=[{"scheme": "CPV", "code": "32323500", "label": "Видео надзор"}]
    )
    session = FakeSession(results=[FakeResult(items=[tender])])
    client = _client(session)
    resp = client.get("/tender/11111111-1111-4111-8111-111111111111")
    assert resp.status_code == 200
    assert "32323500" in resp.text
    assert "Video nadzor" in resp.text
    assert "Видео надзор" not in resp.text


def test_tender_page_long_description_disclosure():
    marker = "УникальныйМаркерТЗ"
    tender = _tender(description=marker + " ТЗ. " * 200)
    session = FakeSession(results=[FakeResult(items=[tender])])
    client = _client(session)
    resp = client.get("/tender/11111111-1111-4111-8111-111111111111?lang=ru")
    assert resp.status_code == 200
    assert "tp-desc--clamp" in resp.text
    assert "Показать полностью" in resp.text
    # ‼️ A CSS-only checkbox hack, and NOT <details>/<summary>: an interactive <summary>
    # turned the whole text of the spec into one button (a mouse selection collapsed the block,
    # the screen reader read the whole description as a “button”, keyboard focus — an element in the screen).
    # The text of the description must NOT lie inside <summary>/<details>.
    assert "<details" not in resp.text and "<summary" not in resp.text
    # A hidden but focusable checkbox + a label-switch.
    assert 'type="checkbox"' in resp.text and "tp-desc__cb" in resp.text
    assert 'for="tp-desc-more"' in resp.text
    # The text of the spec itself is present in the HTML in the CLOSED state (the clamp is CSS only —
    # the former defect: the text dropped out of indexing when closed) and lies in the ordinary
    # <div.tp-desc__full>, outside the interactive element.
    full = re.search(r"<div[^>]*tp-desc__full[^>]*>(.*?)</div>", resp.text, re.S)
    assert full and marker in full.group(1)


def test_tender_page_region_shows_human_label_not_code():
    """RS-09 (the machine code of the region) → the human name; the code stays in the title (#428)."""
    tender = _tender(regions=["RS-09"])
    session = FakeSession(results=[FakeResult(items=[tender])])
    client = _client(session)
    resp = client.get("/tender/11111111-1111-4111-8111-111111111111?lang=ru")
    assert resp.status_code == 200
    assert "Kolubarski" in resp.text
    assert 'title="RS-09"' in resp.text
    # The raw code must not leak into the visible text of the region field.
    region_dd = re.search(
        r'<dt class="tp-fact__label">Регион</dt>\s*<dd[^>]*>(.*?)</dd>', resp.text, re.S
    )
    assert region_dd is not None
    assert "RS-09" not in re.sub(r'title="[^"]*"', "", region_dd.group(1))


def test_tender_page_status_and_procedure_human_labels():
    """status=active/procedure_type=open — the known codes get captions."""
    tender = _tender(status="active", procedure_type="open")
    session = FakeSession(results=[FakeResult(items=[tender])])
    client = _client(session)
    resp = client.get("/tender/11111111-1111-4111-8111-111111111111?lang=ru")
    assert resp.status_code == 200
    assert "Активен" in resp.text
    assert "Открытая процедура" in resp.text
    assert ">active<" not in resp.text
    assert ">open<" not in resp.text


def test_tender_page_unknown_status_shown_as_is():
    """An unknown status code — fail-open: we show it as is, not empty."""
    tender = _tender(status="weird-new-status", procedure_type="weird-procedure")
    session = FakeSession(results=[FakeResult(items=[tender])])
    client = _client(session)
    resp = client.get("/tender/11111111-1111-4111-8111-111111111111?lang=ru")
    assert resp.status_code == 200
    assert "weird-new-status" in resp.text
    assert "weird-procedure" in resp.text


def test_tender_page_404():
    session = FakeSession(results=[FakeResult(items=[])])
    client = _client(session)
    resp = client.get("/tender/missing?lang=ru")
    assert resp.status_code == 404
    assert "Тендер не найден" in resp.text


def test_tender_page_canonical_alts_badge():
    tender = _tender(canonical_tender_id="c-1")
    # 1st execute → get_tender; 2nd → count_canonical_alts
    session = FakeSession(results=[FakeResult(items=[tender]), FakeResult(scalar=3)])
    client = _client(session)
    resp = client.get("/tender/11111111-1111-4111-8111-111111111111?lang=ru")
    assert resp.status_code == 200
    assert "также на 3" in resp.text


def test_tender_page_probable_alts_badge_says_probably():
    """A “probable” link (2–3 points of scoring) — not “the same”, but “probably the same”."""
    tender = _tender(canonical_tender_id="c-1", canonical_confidence="probable")
    session = FakeSession(results=[FakeResult(items=[tender]), FakeResult(scalar=1)])
    client = _client(session)
    resp_ru = client.get("/tender/11111111-1111-4111-8111-111111111111?lang=ru")
    assert "вероятно, тот же тендер ещё на 1 площадках" in resp_ru.text
    teardown_function()

    session = FakeSession(results=[FakeResult(items=[tender]), FakeResult(scalar=1)])
    resp_sr = _client(session).get(
        "/tender/11111111-1111-4111-8111-111111111111?lang=sr"
    )
    assert "Verovatno isti tender je i na još 1 platformi" in resp_sr.text


def test_tender_page_dup_alts_badge_is_plain():
    tender = _tender(canonical_tender_id="c-1", canonical_confidence="dup")
    session = FakeSession(results=[FakeResult(items=[tender]), FakeResult(scalar=1)])
    resp = _client(session).get("/tender/11111111-1111-4111-8111-111111111111?lang=ru")
    assert "также на 1 площадках" in resp.text
    assert "вероятно" not in resp.text


def _name_source(monkeypatch, name="Portal javnih nabavki"):
    """Substitute the source name (in the live code — the ``sources`` table)."""
    from src.api.public import routes_tender

    async def fake_label(_db, key):
        return name if key else ""

    monkeypatch.setattr(routes_tender, "source_label", fake_label)


def test_tender_page_trust_source_and_urgency(monkeypatch):
    """The trust block: the source — of exactly THIS tender + an explanation of the urgency."""
    _name_source(monkeypatch)
    session = FakeSession(results=[FakeResult(items=[_tender(source_key="jnportal")])])
    client = _client(session)
    resp = client.get("/tender/11111111-1111-4111-8111-111111111111?lang=ru")
    assert resp.status_code == 200
    assert "tp-trust" in resp.text
    assert "Данные загружаются с официального источника (Portal javnih nabavki)" in (
        resp.text
    )
    assert "eNabavke" not in resp.text
    # an explanation of how “Urgent” is counted
    assert "7 дней или меньше" in resp.text
    # an internal link to help
    assert 'href="/help"' in resp.text


def test_tender_page_trust_i18n_sr_and_en(monkeypatch):
    """The trust copy is localised (sr/en parity)."""
    _name_source(monkeypatch)
    session = FakeSession(results=[FakeResult(items=[_tender(source_key="jnportal")])])
    client = _client(session)
    resp_sr = client.get("/tender/11111111-1111-4111-8111-111111111111?lang=sr")
    assert "Podaci se preuzimaju sa zvaničnog izvora (Portal javnih nabavki)" in (
        resp_sr.text
    )
    assert "Hitno" in resp_sr.text
    teardown_function()

    session = FakeSession(results=[FakeResult(items=[_tender(source_key="jnportal")])])
    client = _client(session)
    resp_en = client.get("/tender/11111111-1111-4111-8111-111111111111?lang=en")
    assert "Data is pulled from the official source (Portal javnih nabavki)" in (
        resp_en.text
    )


def test_tender_page_shows_source_name_fact_and_button(monkeypatch):
    """SRC-0d: "Izvor: <name>“ and the button ”Otvori na: <name>" instead of a nameless one."""
    _name_source(monkeypatch, "TED")
    session = FakeSession(
        results=[FakeResult(items=[_tender(source_key="ted", country_code="ME")])]
    )
    resp = _client(session).get("/tender/11111111-1111-4111-8111-111111111111?lang=sr")
    assert ">Izvor</dt>" in resp.text
    assert (
        'tp-fact__value"><a href="/izvori#ted" class="underline">TED</a> <small>ME</small>'
        in resp.text
    )
    assert "Otvori na: TED ↗" in resp.text
    assert "Otvori na izvoru" not in resp.text


def test_tender_page_serbian_source_has_no_country_tag(monkeypatch):
    _name_source(monkeypatch)
    session = FakeSession(
        results=[FakeResult(items=[_tender(source_key="jnportal", country_code="RS")])]
    )
    resp = _client(session).get("/tender/11111111-1111-4111-8111-111111111111?lang=sr")
    assert (
        'tp-fact__value"><a href="/izvori#jnportal" class="underline">'
        "Portal javnih nabavki</a></dd>" in resp.text
    )


def test_tender_page_without_source_name_keeps_generic_button(monkeypatch):
    _name_source(monkeypatch, "")
    session = FakeSession(results=[FakeResult(items=[_tender()])])
    resp = _client(session).get("/tender/11111111-1111-4111-8111-111111111111?lang=sr")
    assert "Otvori na izvoru ↗" in resp.text
    assert ">Izvor</dt>" not in resp.text


def test_tender_page_canonical_and_og_url():
    """SEO: canonical + og:url — absolute https from CABINET_BASE_URL, not from
    request.url_for (behind the proxy uvicorn sees http, a fact of 26.09.2026)."""
    session = FakeSession(results=[FakeResult(items=[_tender()])])
    client = _client(session)
    resp = client.get("/tender/11111111-1111-4111-8111-111111111111?lang=ru")
    assert resp.status_code == 200
    assert (
        'rel="canonical" href="https://nab.rs/tender/11111111-1111-4111-8111-111111111111"'
        in resp.text
    )
    assert (
        'property="og:url" content="https://nab.rs/tender/11111111-1111-4111-8111-111111111111"'
        in resp.text
    )
    assert 'property="og:type" content="article"' in resp.text
    assert resp.text.count('rel="canonical"') == 1


def test_tender_page_json_ld_government_service():
    """The JSON-LD of the card: GovernmentService, without a false “seller” and “price” (card-truth, 07.10).

    The customer is not the seller, the estimated value is not the offer price, the application deadline is not validThrough of the service. Only the fields that a Service has by meaning go into the markup.
    """
    tender = _tender(
        classifications=[{"scheme": "CPV", "code": "32323500", "label": "Видео надзор"}]
    )
    session = FakeSession(results=[FakeResult(items=[tender])])
    client = _client(session)
    resp = client.get("/tender/11111111-1111-4111-8111-111111111111?lang=ru")
    assert resp.status_code == 200
    match = re.search(
        r'<script type="application/ld\+json">(.*?)</script>', resp.text, re.S
    )
    assert match, "JSON-LD script not found"
    data = json.loads(match.group(1))
    assert data == {
        "@context": "https://schema.org",
        "@type": "GovernmentService",
        "name": "Видеонаблюдение для школы",
        "url": "https://nab.rs/tender/11111111-1111-4111-8111-111111111111",
        "identifier": "11111111-1111-4111-8111-111111111111",
        "category": "32323500 Видео надзор",
    }
    assert "analysis" not in json.dumps(data)
    assert "match_score" not in json.dumps(data)


def test_tender_page_json_ld_absent_without_title():
    """Without a real title the JSON-LD is not drawn (we do not invent a brand as the name)."""
    tender = _tender(title=None)
    session = FakeSession(results=[FakeResult(items=[tender])])
    client = _client(session)
    resp = client.get("/tender/11111111-1111-4111-8111-111111111111?lang=ru")
    assert resp.status_code == 200
    assert "application/ld+json" not in resp.text


def test_tender_page_seo_text_strips_excel_cr_artifact():
    """The Excel backfill _x000D_ is removed from title/h1/meta/JSON-LD (2943 rows of June)."""
    tender = _tender(
        title="Силиконски гитови_x000D_\n4000/0630/2019_x000D_\nЈана 4023/2019",
        customer_name="ЈКП «Инфостан»_x000D_\nогранак",
    )
    session = FakeSession(results=[FakeResult(items=[tender])])
    client = _client(session)
    resp = client.get("/tender/11111111-1111-4111-8111-111111111111?lang=sr")
    assert resp.status_code == 200
    assert "_x000D_" not in resp.text
    assert (
        '<h1 class="tp-title">Silikonski gitovi 4000/0630/2019 Jana 4023/2019</h1>'
        in resp.text
    )
    assert "JKP «Infostan» ogranak" in resp.text


def test_tender_page_meta_description_enriched_with_amount_and_deadline():
    """The SEO snippet is enriched with the amount and the deadline from the tender data."""
    session = FakeSession(results=[FakeResult(items=[_tender()])])
    client = _client(session)
    resp = client.get("/tender/11111111-1111-4111-8111-111111111111?lang=en")
    assert resp.status_code == 200
    match = re.search(r'<meta name="description" content="([^"]+)"', resp.text)
    assert match, "meta description not found"
    desc = match.group(1)
    assert "€12 345" in desc
    assert "Submission deadline:" in desc


def test_tender_page_shows_ai_when_authed():
    tender = _tender()
    analysis = SimpleNamespace(
        output={"summary": "Подходит под профиль.", "match_score": 4}
    )
    # authed: get_tender (canonical None → without count) → analysis
    session = FakeSession(
        results=[FakeResult(items=[tender]), FakeResult(items=[analysis])]
    )
    client = _client(session, sess={"uid": "u1", "tid": "t1"})
    resp = client.get("/tender/11111111-1111-4111-8111-111111111111?lang=ru")
    assert resp.status_code == 200
    assert "AI-анализ под ваш шаблон" in resp.text
    assert "Подходит под профиль." in resp.text


def _prime_geo():
    import time

    from src.api.public import geo_cache

    geo_cache._cpv_cache = (
        time.monotonic() + 3600,
        {"45": {"total": 60, "open": 5, "open_sum_eur": None, "label": "Radovi"}},
    )
    geo_cache._region_cache = (
        time.monotonic() + 3600,
        {"RS-00": {"total": 80, "open": 9, "open_sum_eur": None}},
    )


def test_tender_page_links_to_category_and_region_hubs():
    """“More in this category / region →”: links to the sections /tenderi."""
    _prime_geo()
    t = _tender(
        classifications=[{"scheme": "CPV", "code": "45233120", "label": "Radovi"}],
        regions=["RS-00"],
    )
    resp = _client(FakeSession(results=[FakeResult(items=[t])])).get(
        "/tender/11111111-1111-4111-8111-111111111111?lang=ru"
    )
    assert 'href="https://nab.rs/tenderi/cpv/45"' in resp.text
    assert 'href="https://nab.rs/tenderi/region/RS-00"' in resp.text


def test_tender_page_without_cpv_and_region_has_no_more_row():
    """There is no CPV and no region — the relinking line is absent altogether (not even an empty nav)."""
    _prime_geo()
    t = _tender(classifications=None, regions=[])
    resp = _client(FakeSession(results=[FakeResult(items=[t])])).get(
        "/tender/11111111-1111-4111-8111-111111111111?lang=ru"
    )
    assert resp.status_code == 200
    assert "/tenderi/cpv/" not in resp.text
    assert "/tenderi/region/" not in resp.text


def test_tender_page_expired_hides_stale_active_status():
    """A deadline in the past + status='active' → the badge «Zatvoreno», but NOT «Aktivan»."""
    tender = _tender(
        status="active", deadline_at=datetime.now(timezone.utc) - timedelta(days=3)
    )
    for lang, closed, active in (
        ("sr", "Zatvoreno", "Aktivan"),
        ("ru", "Закрыт", "Активен"),
        ("en", "Closed", "Active"),
    ):
        session = FakeSession(results=[FakeResult(items=[tender])])
        resp = _client(session).get(
            f"/tender/11111111-1111-4111-8111-111111111111?lang={lang}"
        )
        assert resp.status_code == 200
        assert closed in resp.text
        assert f">{active}<" not in resp.text.replace("\n", "").replace("  ", "")


def test_tender_page_future_deadline_keeps_active_status():
    """A deadline in the future → «Aktivan» stays."""
    session = FakeSession(results=[FakeResult(items=[_tender(status="active")])])
    resp = _client(session).get("/tender/11111111-1111-4111-8111-111111111111?lang=sr")
    assert resp.status_code == 200
    assert "Aktivan" in resp.text


def test_tender_page_trust_does_not_promise_live_updates():
    """There is no promise that the changes of the tender are “visible on the same card”."""
    session = FakeSession(results=[FakeResult(items=[_tender()])])
    resp = _client(session).get("/tender/11111111-1111-4111-8111-111111111111?lang=sr")
    assert "vidite na istoj kartici" not in resp.text
    assert "izvornom portalu" in resp.text


def test_no_deadline_stale_tender_is_not_shown_as_active():
    """Without a deadline and older than 30 d. “Aktivan” is a lie: the status is not confirmed (card-truth, 07.10).

    The prod `/tender/2337603f-…`: “Rok nije naveden” + “Aktivan” with a publication of long ago. The expectation as a literal from the fact, not via t().
    """
    old = datetime.now(timezone.utc) - timedelta(days=60)
    tender = _tender(deadline_at=None, published_at=old, first_seen_at=old)
    session = FakeSession(results=[FakeResult(items=[tender])])
    client = _client(session)
    resp = client.get("/tender/11111111-1111-4111-8111-111111111111?lang=sr")
    assert resp.status_code == 200
    assert "Nije naveden" in resp.text
    assert "Aktivan" not in resp.text
    assert "Status nije potvrđen" in resp.text


def test_no_deadline_fresh_tender_keeps_active_status():
    """Without a deadline, but younger than 30 d. — the status “Aktivan” stays (the actuality rule)."""
    fresh = datetime.now(timezone.utc) - timedelta(days=5)
    tender = _tender(deadline_at=None, published_at=fresh, first_seen_at=fresh)
    session = FakeSession(results=[FakeResult(items=[tender])])
    client = _client(session)
    resp = client.get("/tender/11111111-1111-4111-8111-111111111111?lang=sr")
    assert resp.status_code == 200
    assert "Aktivan" in resp.text
    assert "Status nije potvrđen" not in resp.text


def test_stale_no_deadline_status_unconfirmed_all_languages():
    old = datetime.now(timezone.utc) - timedelta(days=60)
    tender = _tender(deadline_at=None, published_at=old, first_seen_at=old)
    session = FakeSession(results=[FakeResult(items=[tender])])
    client = _client(session)
    assert (
        "Статус не подтверждён"
        in client.get("/tender/11111111-1111-4111-8111-111111111111?lang=ru").text
    )
    teardown_function()
    session = FakeSession(results=[FakeResult(items=[tender])])
    client = _client(session)
    assert (
        "Status not confirmed"
        in client.get("/tender/11111111-1111-4111-8111-111111111111?lang=en").text
    )


class _UuidRejectingSession:
    """As the prod DB on a non-UUID in ``CAST(... AS uuid)``: the query falls with ``DataError``.

    On 07.10 ``GET /tender/not-a-uuid`` on production gave a 500 exactly so.
    """

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def execute(self, *args, **kwargs):
        raise DataError(
            "SELECT tenders_normalized",
            {},
            Exception("invalid input syntax for type uuid"),
        )


def test_non_uuid_tender_id_is_404_not_500():
    """An invalid id → a 404 page, not a 500 from the DB (card-truth, 07.10)."""
    _client(_UuidRejectingSession())
    client = TestClient(app, raise_server_exceptions=False)
    resp = client.get("/tender/not-a-uuid")
    assert resp.status_code == 404


def _award(**kw):
    base = dict(
        winner_name="DOO Primer Beograd",
        award_amount_eur=8500.0,
        decision_date=date(2026, 3, 12),
        lot_number=None,
        lot_title=None,
    )
    base.update(kw)
    return SimpleNamespace(**base)


def test_tender_page_shows_award_when_present(monkeypatch):
    """There is an award decision — the block “Odluka o dodeli” with the supplier, the amount, the date.

    Literals from the fact: the supplier, “€8 500”, “12.03.2026”.
    """
    monkeypatch.setattr(
        repo, "get_tender_awards", _async_value([_award()]), raising=False
    )
    session = FakeSession(results=[FakeResult(items=[_tender()])])
    client = _client(session)
    resp = client.get("/tender/11111111-1111-4111-8111-111111111111?lang=sr")
    assert resp.status_code == 200
    assert "Odluka o dodeli" in resp.text
    assert "Izabrani ponuđač" in resp.text
    assert "DOO Primer Beograd" in resp.text
    assert "€8 500" in resp.text
    assert "12.03.2026" in resp.text


def test_tender_page_no_award_block_without_award(monkeypatch):
    monkeypatch.setattr(repo, "get_tender_awards", _async_value([]), raising=False)
    session = FakeSession(results=[FakeResult(items=[_tender()])])
    client = _client(session)
    resp = client.get("/tender/11111111-1111-4111-8111-111111111111?lang=sr")
    assert resp.status_code == 200
    assert "Odluka o dodeli" not in resp.text
    assert "Izabrani ponuđač" not in resp.text


def test_awards_query_has_no_future_decision_predicate():
    """The predicate “the decision is not from the future” stands in the SQL (the name is about the text of the query, not about the data)."""
    captured = {}

    class _Capture(FakeSession):
        async def execute(self, statement, params=None):
            captured["sql"] = str(statement)
            return FakeResult(items=[])

    import asyncio

    asyncio.run(
        repo.get_tender_awards(
            _Capture(), "11111111-1111-4111-8111-111111111111", date(2026, 10, 7)
        )
    )
    assert "tender_awards.decision_date <=" in captured["sql"]
    assert "tender_awards.winner_name IS NOT NULL" in captured["sql"]


def test_tender_page_without_eur_shows_original_amount_with_currency():
    """TED/MKD: amount_eur NULL, the original exists → “12 000 000 MKD”, not “not stated”."""
    tender = _tender(
        amount_eur=None, amount_original=12000000.0, currency_original="MKD"
    )
    session = FakeSession(results=[FakeResult(items=[tender])])
    resp = _client(session).get("/tender/11111111-1111-4111-8111-111111111111?lang=en")
    assert resp.status_code == 200
    assert "12 000 000 MKD" in resp.text
    assert "not specified" not in resp.text
    desc = re.search(r'<meta name="description" content="([^"]+)"', resp.text).group(1)
    assert "12 000 000 MKD" in desc


def test_tender_page_without_any_amount_still_says_not_specified():
    tender = _tender(amount_eur=None, amount_original=None, currency_original=None)
    session = FakeSession(results=[FakeResult(items=[tender])])
    resp = _client(session).get("/tender/11111111-1111-4111-8111-111111111111?lang=en")
    assert "not specified" in resp.text


class _UuidStrictSession(FakeSession):
    """As asyncpg does in production: a uuid parameter not accepted by the ``uuid`` codec → ``DataError``.

    On 08.10 ``/tender/urn:uuid:…`` and ``/tender/{…}`` gave a 500 exactly so: the raw string went to the DB. We accept only the canonical form (36 characters).
    """

    def __init__(self):
        super().__init__(results=[FakeResult(items=[_tender()])])

    async def execute(self, statement, params=None):
        for value in statement.compile().params.values():
            if isinstance(value, str) and len(value) != 36:
                raise DataError(
                    "SELECT tenders_normalized",
                    {},
                    Exception(f"invalid UUID {value!r}: length {len(value)}"),
                )
        return await super().execute(statement, params)


def test_urn_and_braced_tender_id_redirect_to_canonical_not_500():
    """One canonical link (SEO): ``urn:uuid:…`` and ``{…}`` → 301 to ``str(UUID)``."""
    _client(_UuidStrictSession())
    client = TestClient(app, raise_server_exceptions=False, follow_redirects=False)
    urn = "urn:uuid:11111111-1111-4111-8111-111111111111"
    resp = client.get(f"/tender/{urn}")
    assert resp.status_code == 301
    assert resp.headers["location"] == "/tender/11111111-1111-4111-8111-111111111111"

    resp = client.get("/tender/{11111111-1111-4111-8111-111111111111}?lang=ru")
    assert resp.status_code == 301
    assert resp.headers["location"] == (
        "/tender/11111111-1111-4111-8111-111111111111?lang=ru"
    )


def test_uppercase_uuid_redirects_to_lowercase_canonical():
    _client(_UuidStrictSession())
    client = TestClient(app, raise_server_exceptions=False, follow_redirects=False)
    resp = client.get("/tender/AAAAAAAA-1111-4111-8111-111111111111")
    assert resp.status_code == 301
    assert resp.headers["location"] == "/tender/aaaaaaaa-1111-4111-8111-111111111111"


def test_canonical_tender_id_is_served_directly():
    _client(_UuidStrictSession())
    client = TestClient(app, raise_server_exceptions=False, follow_redirects=False)
    resp = client.get("/tender/11111111-1111-4111-8111-111111111111")
    assert resp.status_code == 200


def test_three_awards_show_one_section_heading(monkeypatch):
    """On 08.10 at /tender/00b615bc-… three identical “Odluka o dodeli” (28% of cards with decisions).

    The heading — one above the list; each decision has a distinguisher: the lot number (``Lot 6``) or the ordinal number (``Stavka 3``), if there is no lot.
    """
    awards = [
        _award(
            lot_number="6",
            lot_title="Средства за дезинфекцију",
            winner_name="TREN DOO NIS",
            award_amount_eur=1340.0,
            decision_date=date(2026, 9, 15),
        ),
        _award(
            lot_number="7",
            lot_title="Треке за контролу стерилизације",
            winner_name="ENGEL DOO NOVI SAD",
            award_amount_eur=1340.0,
            decision_date=date(2026, 9, 15),
        ),
        _award(
            winner_name="PAN STAR DOO NOVI SAD",
            award_amount_eur=None,
            decision_date=date(2026, 9, 15),
        ),
    ]
    monkeypatch.setattr(repo, "get_tender_awards", _async_value(awards), raising=False)
    session = FakeSession(results=[FakeResult(items=[_tender()])])
    client = _client(session)
    resp = client.get("/tender/11111111-1111-4111-8111-111111111111?lang=sr")
    assert resp.status_code == 200
    assert resp.text.count('<h2 class="tp-section__title">Odluka o dodeli</h2>') == 1
    assert resp.text.count("Izabrani ponuđač") == 3
    assert "Lot 6" in resp.text
    assert "Lot 7" in resp.text
    assert "Stavka 3" in resp.text


def test_single_award_has_no_item_marker(monkeypatch):
    """One decision — without a distinguisher (the page as it was)."""
    monkeypatch.setattr(
        repo, "get_tender_awards", _async_value([_award()]), raising=False
    )
    session = FakeSession(results=[FakeResult(items=[_tender()])])
    client = _client(session)
    resp = client.get("/tender/11111111-1111-4111-8111-111111111111?lang=sr")
    assert resp.text.count('<h2 class="tp-section__title">Odluka o dodeli</h2>') == 1
    assert "Stavka 1" not in resp.text
    assert "Lot " not in resp.text


def test_tender_page_ai_analysis_carries_disclaimer_once(monkeypatch):
    """The public card: the AI analysis for a logged-in user — with the caveat exactly once (audit 08.10)."""
    analysis = SimpleNamespace(output={"match_score": 4, "summary": "Резиме"})
    monkeypatch.setattr(repo, "get_tender_ai_analysis", _async_value(analysis))
    session = FakeSession(results=[FakeResult(items=[_tender()])])
    client = _client(session, sess={"uid": "u-1", "tid": "t-1"})
    resp = client.get("/tender/11111111-1111-4111-8111-111111111111?lang=ru")
    assert resp.status_code == 200
    assert "Резиме" in resp.text
    assert (
        resp.text.count("AI может ошибаться — сверяйте с документацией закупки.") == 1
    )


def test_tender_page_cta_subtitle_no_false_per_tender_promise():
    """The conversion CTA does not promise “a ready answer for every procurement” (16.10).

    The AI analysis is limited by the plan/trial limit (``TRIAL_AUTO_DEMO_LIMIT``), all tenders arrive, the analysis — not for each. The literals — from the fact, not via ``t()``.
    """
    url = "/tender/11111111-1111-4111-8111-111111111111"
    cases = {
        "sr": (
            "Podesite šablon pretrage jednom — odgovarajuće nabavke stižu u Telegram, "
            "a one važne možete analizirati uz AI.",
            "za svaku relevantnu nabavku",
        ),
        "ru": (
            "Настройте шаблон поиска один раз — подходящие закупки приходят в Telegram, "
            "а нужные можно разобрать с AI.",
            "по каждой релевантной",
        ),
        "en": (
            "Set up your search template once: matching tenders arrive in Telegram, "
            "with an AI review of the ones that matter.",
            "for every relevant tender",
        ),
    }
    for lang, (fresh, stale) in cases.items():
        session = FakeSession(results=[FakeResult(items=[_tender()])])
        client = _client(session)
        resp = client.get(f"{url}?lang={lang}")
        assert resp.status_code == 200, lang
        assert fresh in resp.text, lang
        assert stale not in resp.text.lower(), lang
        teardown_function()
