"""The product news feed ``/novosti`` and the “Novo” label in the cabinet (owner 10.10.2026).

Expectations — LITERALS (not via the catalogs/``news.NEWS`` that the code uses).
"""

from __future__ import annotations

import re

import pytest
from fastapi.testclient import TestClient

from src.api import deps, security
from src.api.cabinet import routes_profiles
from src.api.config import get_cabinet_settings
from src.api.main import app
from tests.api.conftest import FakeSession, make_session_factory

_UID = "eeee0000-0000-0000-0000-000000000001"
_TID = "ffff0000-0000-0000-0000-000000000002"
_SESSION = {"uid": _UID, "tid": _TID}


# The anchor of the external link: href and rel/target are separated by the class (class — between them).
_TG_ANCHOR = re.compile(
    r'<a href="https://t\.me/nabrs_news"[^>]* target="_blank" rel="noopener"[^>]*>'
)


def teardown_function():
    app.dependency_overrides.clear()


def _async(value):
    async def _stub(*args, **kwargs):
        return value

    return _stub


@pytest.fixture(autouse=True)
def _stub_dashboard(monkeypatch):
    monkeypatch.setattr(routes_profiles.svc, "list_profiles", _async([]))
    monkeypatch.setattr(routes_profiles.svc, "downgrade_notice", _async([]))
    monkeypatch.setattr(
        routes_profiles.channel_svc, "has_active_telegram", _async(False)
    )


def _client(*, authed: bool = False, locale: str | None = None) -> TestClient:
    fs = FakeSession(results=[])
    app.dependency_overrides[deps.get_session_factory] = lambda: make_session_factory(
        fs
    )
    app.dependency_overrides[deps.get_optional_session] = lambda: (
        _SESSION if authed else None
    )
    client = TestClient(app, follow_redirects=False)
    if authed:
        secret = get_cabinet_settings().cabinet_secret_key
        client.cookies.set(
            "tb_session", security.sign_session(_UID, _TID, secret, locale=locale)
        )
    return client


# --- public feed ---------------------------------------------------------


def test_list_sr_default_latin_only():
    r = _client().get("/novosti")
    assert r.status_code == 200
    assert "Statistika tržišta javnih nabavki" in r.text
    assert "Analitika i izveštaji u Excel-u" in r.text
    assert "Sve na latinici" in r.text
    assert "10.10.2026" in r.text
    assert not re.search(r"[Ѐ-ӿ]", r.text)


def test_list_ru_and_en():
    ru = _client().get("/novosti?lang=ru").text
    assert "Статистика рынка госзакупок" in ru
    assert "Аналитика и отчёты в Excel" in ru
    assert "Всё на латинице" in ru
    en = _client().get("/novosti?lang=en").text
    assert "Public procurement market statistics" in en
    assert "Analytics and reports in Excel" in en
    assert "Everything in Latin script" in en


def test_stats_item_describes_period_once_not_per_figure():
    # The period is shown once at the top of /statistika (data-stats-period),
    # and not for each figure — the text of the announcement must say so, not promise otherwise.
    sr = _client().get("/novosti/statistika-trzista").text
    assert "Na stranici su navedeni datum početka podataka i datum stanja." in sr
    en = _client().get("/novosti/statistika-trzista?lang=en").text
    assert (
        "The page states when the data starts and the date it is current as of." in en
    )
    ru = _client().get("/novosti/statistika-trzista?lang=ru").text
    assert (
        "На странице указаны дата начала данных и дата, на которую они актуальны." in ru
    )


def test_item_page_seo_hreflang_og_all_languages():
    for lang, title in (
        ("sr", "Analitika i izveštaji u Excel-u"),
        ("ru", "Аналитика и отчёты в Excel"),
        ("en", "Analytics and reports in Excel"),
    ):
        r = _client().get(f"/novosti/analitika-excel?lang={lang}")
        assert r.status_code == 200
        assert f'<meta property="og:title" content="{title}">' in r.text
        assert (
            'hreflang="x-default" href="https://nab.rs/novosti/analitika-excel"'
            in r.text
        )
        assert (
            'hreflang="ru" href="https://nab.rs/novosti/analitika-excel?lang=ru"'
            in r.text
        )
        assert '"@type": "NewsArticle"' in r.text or '"@type":"NewsArticle"' in r.text
        assert "2026-10-10" in r.text
    sr = _client().get("/novosti/analitika-excel")
    assert 'rel="canonical" href="https://nab.rs/novosti/analitika-excel"' in sr.text
    assert not re.search(r"[Ѐ-ӿ]", sr.text)


def test_item_unknown_slug_404():
    assert _client().get("/novosti/ne-postoji").status_code == 404


def test_sitemap_has_novosti_all_languages():
    r = _client().get("/sitemap-pages.xml")
    for path in (
        "/novosti",
        "/novosti/kabinet-treci-kanal",
        "/novosti/rokovi-tacno",
        "/novosti/svetska-banka",
        "/novosti/kalendar-rokova",
        "/novosti/telegram-kanal",
        "/novosti/statistika-trzista",
        "/novosti/analitika-excel",
        "/novosti/sve-na-latinici",
    ):
        assert f"<loc>https://nab.rs{path}</loc>" in r.text
        assert f"<loc>https://nab.rs{path}?lang=ru</loc>" in r.text
        assert f"<loc>https://nab.rs{path}?lang=en</loc>" in r.text


# --- Cabinet as the third channel and exact deadlines (19.10.2026) --------------------


def test_cabinet_channel_and_deadlines_items_200_all_languages():
    cases = (
        (
            "/novosti/kabinet-treci-kanal",
            "Kabinet je treći kanal za tendere",
            "na stranici „Moja poklapanja“",
        ),
        (
            "/novosti/kabinet-treci-kanal?lang=en",
            "The cabinet is a third channel for tenders",
            "on the “My matches” page",
        ),
        (
            "/novosti/kabinet-treci-kanal?lang=ru",
            "Кабинет: третий канал получения тендеров",
            "на странице «Мои совпадения»",
        ),
        (
            "/novosti/rokovi-tacno",
            "Tačni rokovi za narednih 7 dana",
            "bez ograničenja na 50 stavki",
        ),
        (
            "/novosti/rokovi-tacno?lang=en",
            "Exact deadlines for the next 7 days",
            "with no cap at 50 items",
        ),
        (
            "/novosti/rokovi-tacno?lang=ru",
            "Точные сроки на ближайшие 7 дней",
            "без ограничения в 50 записей",
        ),
    )
    for path, title, fact in cases:
        r = _client().get(path)
        assert r.status_code == 200
        assert title in r.text
        assert fact in r.text
        assert "2026-10-19" in r.text


def test_cabinet_channel_body_literal_sr_and_no_retro_claim():
    sr = _client().get("/novosti/kabinet-treci-kanal").text
    assert "stariji se ne pojavljuju unazad" in sr
    assert "stari se ne šalju u paketu" in sr
    assert "Moja poklapanja" in sr


def test_deadlines_body_literal_sr_belgrade_calendar():
    sr = _client().get("/novosti/rokovi-tacno").text
    assert "Ističe za 7 dana" in sr
    assert "po kalendaru u Beogradu" in sr
    assert (
        "Tender kojem rok ističe sutra ujutro tako uvek ulazi u prvih sedam dana" in sr
    )


# --- World Bank and “To calendar” (17.10.2026) ------------------------------


def test_worldbank_and_calendar_items_200_all_languages():
    cases = (
        (
            "/novosti/svetska-banka",
            "Novi izvor: Svetska banka (Zapadni Balkan)",
            "Na dan 17.10. otvoreno je 12 tendera",
        ),
        (
            "/novosti/svetska-banka?lang=en",
            "New source: World Bank (Western Balkans)",
            "As of 17.10., 12 tenders are open",
        ),
        (
            "/novosti/svetska-banka?lang=ru",
            "Новая площадка: Всемирный банк (Западные Балканы)",
            "На 17.10 открыто 12 тендеров",
        ),
        (
            "/novosti/kalendar-rokova",
            "Rok podnošenja u vaš kalendar",
            "„📅 U kalendar“",
        ),
        (
            "/novosti/kalendar-rokova?lang=en",
            "Submission deadline in your calendar",
            "“📅 Add to calendar”",
        ),
        (
            "/novosti/kalendar-rokova?lang=ru",
            "Срок подачи в ваш календарь",
            "«📅 В календарь»",
        ),
    )
    for path, title, fact in cases:
        r = _client().get(path)
        assert r.status_code == 200
        assert title in r.text
        assert fact in r.text


# --- Telegram channel (owner 14.10.2026) -----------------------------------------


def test_telegram_item_200_all_languages():
    for path, title in (
        ("/novosti/telegram-kanal", "Telegram kanal TenderSRB"),
        ("/novosti/telegram-kanal?lang=ru", "Telegram-канал TenderSRB"),
        ("/novosti/telegram-kanal?lang=en", "TenderSRB Telegram channel"),
    ):
        r = _client().get(path)
        assert r.status_code == 200
        assert f'<meta property="og:title" content="{title}">' in r.text
        assert _TG_ANCHOR.search(r.text)


def test_telegram_item_body_and_link_label():
    sr = _client().get("/novosti/telegram-kanal").text
    assert (
        "Otvorili smo Telegram kanal @nabrs_news: novosti o servisu, nove funkcije i beta test."
        in sr
    )
    assert "Otvori kanal</a>" in sr
    ru = _client().get("/novosti/telegram-kanal?lang=ru").text
    assert (
        "Мы открыли Telegram-канал @nabrs_news: новости сервиса, новые функции и бета-тест."
        in ru
    )
    assert "Открыть канал</a>" in ru
    en = _client().get("/novosti/telegram-kanal?lang=en").text
    assert (
        "We have opened the Telegram channel @nabrs_news: service news, new features and the beta test."
        in en
    )
    assert "Open channel</a>" in en


def test_telegram_is_first_in_list_and_external_link_in_list():
    r = _client().get("/novosti").text
    assert r.index("Telegram kanal TenderSRB") < r.index(
        "Statistika tržišta javnih nabavki"
    )
    assert _TG_ANCHOR.search(r)


def test_internal_links_unchanged():
    r = _client().get("/novosti").text
    assert 'href="/statistika" class="font-medium' in r
    assert 'href="/statistika" target' not in r
    assert 'href="/app/analitika" class="font-medium' in r
    item = _client().get("/novosti/analitika-excel").text
    assert 'href="/app/analitika" class="btn-primary' in item
    # An internal link — without target. The whole page `target="_blank"` contains:
    # the external link to the TG channel in the footer (#714), so we check the anchor itself.
    assert 'href="/app/analitika" target' not in item
    assert not re.search(r'<a href="/app/analitika"[^>]*target=', item)


# --- New notice types and PIN (owner 15.10.2026) -----------------------------


def test_new_types_item_200_all_languages():
    for path, title in (
        (
            "/novosti/prethodna-obavestenja",
            "Novi tipovi objava u TenderSRB-u",
        ),
        (
            "/novosti/prethodna-obavestenja?lang=ru",
            "Новые типы объявлений в TenderSRB",
        ),
        (
            "/novosti/prethodna-obavestenja?lang=en",
            "New notice types in TenderSRB",
        ),
    ):
        r = _client().get(path)
        assert r.status_code == 200
        assert f'<meta property="og:title" content="{title}">' in r.text
        assert "2026-10-15" in r.text


def test_new_types_body_literal_sr():
    sr = _client().get("/novosti/prethodna-obavestenja").text
    assert "prethodna informativna obaveštenja (PIN)" in sr
    assert "obaveštenja o odbrani i bezbednosti (F17)" in sr
    assert "PIN i sistem kvalifikacije nemaju rok za podnošenje ponuda" in sr
    assert "kartica za njih navodi da rok nije naveden" in sr
    assert "Pregledaj tendere</a>" in sr


def test_new_types_body_literal_ru_en():
    ru = _client().get("/novosti/prethodna-obavestenja?lang=ru").text
    assert "предварительные информационные уведомления (PIN)" in ru
    assert "У PIN и системы квалификации нет срока подачи" in ru
    assert "в карточке для них стоит «срок не указан»" in ru
    en = _client().get("/novosti/prethodna-obavestenja?lang=en").text
    assert "prior information notices (PIN)" in en
    assert "PIN and qualification systems have no submission deadline" in en
    assert "the card says the deadline is not specified for them" in en


def test_new_types_is_first_in_list():
    r = _client().get("/novosti").text
    assert r.index("Novi tipovi objava u TenderSRB-u") < r.index(
        "Telegram kanal TenderSRB"
    )


def test_new_types_in_sitemap():
    r = _client().get("/sitemap-pages.xml")
    assert "<loc>https://nab.rs/novosti/prethodna-obavestenja</loc>" in r.text
    assert "<loc>https://nab.rs/novosti/prethodna-obavestenja?lang=en</loc>" in r.text


def test_head_novosti_ok():
    assert _client().head("/novosti").status_code == 200


# --- “Novo” in the cabinet and “read” -----------------------------------------


def test_unread_badge_and_banner_for_authed_user():
    r = _client(authed=True, locale="sr").get("/app?lang=sr")
    assert r.status_code == 200
    assert "data-news-badge" in r.text
    assert ">Novo</span>" in r.text
    # v2 (owner 13.10): the top banner “Novo u TenderSRB-u” and the closing form are gone —
    # the “Novo” label stands on the Vesti heading, and the entry — in the Vesti list itself.
    assert "data-news-banner" not in r.text
    assert "data-news-new" in r.text
    assert 'id="vesti"' in r.text
    # Preview — 3 newest (NEWS_PREVIEW_LIMIT): news of 20.10 — the first three.
    assert "Bosna i Hercegovina: novi izvor e-Nabavke" in r.text
    assert 'href="/novosti/bih-tenderi"' in r.text
    assert "AI čita svu dokumentaciju tendera" in r.text
    assert 'href="/novosti/ai-cita-sve-dokumente"' in r.text
    assert "Otvoreni kod: snimak izvornog koda na GitHub-u" in r.text
    assert 'href="/novosti/otvoreni-kod"' in r.text
    assert 'href="/novosti/kabinet-treci-kanal"' not in r.text
    assert 'href="/novosti/kalendar-rokova"' not in r.text
    assert 'href="/novosti/prethodna-obavestenja"' not in r.text


def test_banner_localized_ru():
    r = _client(authed=True, locale="ru").get("/app")
    assert "Новое в TenderSRB:" not in r.text  # the top banner is removed
    assert "Босния и Герцеговина: новая площадка e-Nabavke" in r.text
    assert "data-news-new>Новое</span>" in r.text


def test_anon_never_sees_badge():
    r = _client().get("/novosti")
    assert "data-news-badge" not in r.text


def test_badge_disappears_after_viewing_novosti():
    c = _client(authed=True, locale="sr")
    assert "data-news-badge" in c.get("/app?lang=sr").text
    seen = c.get("/novosti")
    assert "news_seen=2026-10-20" in seen.headers["set-cookie"]
    c.cookies.set("news_seen", "2026-10-20")
    after = c.get("/app?lang=sr")
    assert "data-news-badge" not in after.text
    assert "data-news-new" not in after.text


def test_dismiss_form_sets_cookie_and_redirects_back():
    r = _client(authed=True).post("/novosti/procitano", data={"return_to": "/app"})
    assert r.status_code == 303
    assert r.headers["location"] == "/app"
    assert "news_seen=2026-10-20" in r.headers["set-cookie"]


@pytest.mark.parametrize("bad", ["//evil.example", "https://evil.example", "\\\\x"])
def test_dismiss_rejects_open_redirect(bad):
    r = _client(authed=True).post("/novosti/procitano", data={"return_to": bad})
    assert r.headers["location"] == "/app"


def test_garbage_cookie_counts_as_unread():
    c = _client(authed=True, locale="sr")
    c.cookies.set("news_seen", "zzz")
    assert "data-news-badge" in c.get("/app?lang=sr").text


def test_older_cookie_still_unread():
    c = _client(authed=True, locale="sr")
    c.cookies.set("news_seen", "2026-10-09")
    assert "data-news-badge" in c.get("/app?lang=sr").text
