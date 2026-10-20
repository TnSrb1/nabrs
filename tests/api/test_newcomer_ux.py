"""The showcase through a newcomer’s eyes (newcomer_ux): one truth about the start on the render.

Expectations — LITERALS from the brief/fact (verdict B, REPORT_newcomer_ux), not via the
catalog public.i18n: an assertion through the same catalog would be a tautology.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from src.api import deps
from src.api.main import app
from tests.api.conftest import FakeSession


def _client():
    app.dependency_overrides[deps.get_session_factory] = lambda: FakeSession
    app.dependency_overrides[deps.get_optional_session] = lambda: None
    return TestClient(app, follow_redirects=False)


def teardown_function():
    from src.api.public import geo_cache

    app.dependency_overrides.clear()
    geo_cache.clear_cache()  # the cache of the home facts — in-process, does not leak to neighbours


_LANDING = {
    "sr": (
        "Javne nabavke u Srbiji — samo one koje su za vas",
        "30 dana Pro besplatno · samo email, bez kartice",
        "Posle probnog perioda slanje staje, a kabinet ostaje otvoren; "
        "produženje do 30 dana možete zatražiti zahtevom iz kabineta — odluku donosi operater.",
        "Uključeno u Pro (400 mesečno) i Business (1500). U probnom periodu — "
        "demo: 10 automatskih AI-analiza tendera za ceo probni period, zatim samo na plaćenom tarifu.",
        "Dobijate poklapanja u Telegramu ili na email-u i u kabinetu.",
    ),
    "ru": (
        "Госзакупки Сербии — только те, что подходят вам",
        "30 дней Pro бесплатно · только email, без карты",
        "После пробного периода рассылка останавливается, а кабинет "
        "остаётся открытым; продление до 30 дней — заявкой из кабинета, решение принимает оператор.",
        "Входит в Pro (400 в месяц) и Business (1500). В пробном периоде — "
        "демо: 10 авто-разборов тендеров на весь пробный период, дальше — только на платном тарифе.",
    ),
    "en": (
        "Serbian public tenders — only the ones that fit you",
        "30 days of Pro free · email only, no card",
        "After the trial delivery stops and the cabinet stays open; you can request an extension of up to 30 days from the cabinet — the operator decides.",
        "Included in Pro (400 a month) and Business (1500). During the trial — "
        "a demo: 10 automatic tender analyses for the whole trial period, then only on a paid plan.",
    ),
}


@pytest.mark.parametrize("lang", ["sr", "ru", "en"])
def test_landing_states_trial_terms(lang):
    html = _client().get(f"/?lang={lang}").text
    for literal in _LANDING[lang]:
        assert literal in html, (lang, literal)
    # The old false/vague promise is gone.
    assert "Trenutno je probni pristup besplatan" not in html


_PLANS = {
    "sr": (
        "Svaki novi nalog dobija 30 dana Pro besplatno.",
        "Obaveštenja na email",
        "Pošaljite zahtev — javićemo vam se sa uslovima plaćanja.",
        "AI-odabir: 400 mesečno je na plaćenom Pro. U probnom periodu — demo: 10 automatskih analiza tendera za ceo probni period.",
        "Analitika i izveštaji (Excel) na Pro su označeni crticom, ali su u probnom periodu dostupni.",
        "produžiti do 30 dana jednim zahtevom iz kabineta (odluku donosi operater)",
    ),
    "ru": (
        "Каждый новый аккаунт получает 30 дней Pro бесплатно.",
        "Уведомления на email",
        "Отправьте заявку — мы свяжемся с условиями оплаты.",
        "Подбор AI: 400 в месяц — на платном Pro. В пробном периоде — демо: 10 авто-разборов тендеров на весь пробный период.",
        "Аналитика и отчёты (Excel) на Pro отмечены прочерком, но в пробном периоде доступны.",
        # Renewal: an application from the cabinet, the operator’s decision, up to 30 days (FAQ).
        "продлить до 30 дней одной заявкой из кабинета — решение принимает оператор",
    ),
    "en": (
        "Every new account gets 30 days of Pro for free.",
        "Email notifications",
        "Send a request — we will contact you with payment terms.",
        "AI matching: 400 a month is on the paid Pro. During the trial — a demo: 10 automatic tender analyses for the whole trial period.",
        "show a dash on Pro, but are available during the trial.",
        "request an extension of delivery by up to 30 days with one request from the cabinet",
    ),
}


@pytest.mark.parametrize("lang", ["sr", "ru", "en"])
def test_plans_states_trial_and_email_row(lang):
    html = _client().get(f"/plans?lang={lang}").text
    for literal in _PLANS[lang]:
        assert literal in html, (lang, literal)
    assert "Free je samo praćenje" not in html
    assert "Free is tracking only" not in html
    # There is no public Free (owner decision 30.09): neither a card nor a €0 price.
    assert "€0" not in html
    assert "Free" not in html


def test_help_quick_start_mentions_email_and_trial_telegram():
    html = _client().get("/help?lang=sr").text
    assert "Povežite Telegram ili email" in html
    assert "Telegram je" in html and "dostupan na Pro i u probnom periodu." in html
    # The Free/Pro help table no longer promises free Telegram and alerts.
    assert "Praćenje tendera i alertovi" not in html


# --------------------------------------------------------------------------- #
# Step 3 (revised SRC-0d, owner 30.09 R7): “one portal” is no longer promised — the names
# of the sources on the showcase are rendered from the ``sources`` table (placeholder ``{sources}``)
# --------------------------------------------------------------------------- #
_HARDCODED_SINGLE_PORTAL_CLAIMS = (
    "skenira portal javnih nabavki",
    "proveravamo portal javnih nabavki",
    "сканирует портал госзакупок",
    "проверяем портал госзакупок",
    "check the public procurement portal",
    "scans the public procurement portal",
    "scans the procurement portal",
    "источник: портал госзакупок",
    "izvor: portal javnih nabavki",
    "source: serbian public procurement portal",
)


def test_public_catalog_has_no_hardcoded_single_portal_claim():
    """A predicate over the WHOLE showcase catalog file + templates, not over one key."""
    from pathlib import Path

    root = Path(__file__).resolve().parents[2] / "src" / "api"
    sources = [
        root / "public" / "i18n.py",
        *sorted((root / "templates").glob("*.html")),
    ]
    for path in sources:
        text = path.read_text(encoding="utf-8").lower()
        for claim in _HARDCODED_SINGLE_PORTAL_CLAIMS:
            assert claim not in text, (path.name, claim)


def test_tender_trust_updated_literal():
    from src.api.public.i18n import TENDER

    assert TENDER["sr"]["trust_updated"] == (
        "Podaci se preuzimaju sa portala prilikom objave; izmene roka i "
        "dokumentacije pratite na izvornom portalu."
    )
    assert TENDER["en"]["trust_updated"] == (
        "Data is taken from the portal at publication; check the source "
        "portal for changes to the deadline and documents."
    )
    assert TENDER["ru"]["trust_updated"] == (
        "Данные берутся с портала при публикации; изменения срока и "
        "документации смотрите на исходном портале."
    )


# --------------------------------------------------------------------------- #
# Step 6: live figures and a card example on the home page
# --------------------------------------------------------------------------- #
def _example_row():
    from datetime import datetime, timedelta, timezone
    from types import SimpleNamespace

    return SimpleNamespace(
        id="5d77eebf-5531-4645-826a-f7378bf80950",
        title="Nabavka kancelarijskog materijala",
        customer_name="Opština Primer",
        deadline_at=datetime.now(timezone.utc) + timedelta(days=20),
        amount_eur=12345.0,
    )


def _fake_sources(monkeypatch, items):
    from src.api.public import routes_public
    from src.api.public.sources_view import SourceItem, SourcesView

    async def fake_view(_db):
        return SourcesView(tuple(SourceItem(n, c) for n, c in items))

    monkeypatch.setattr(routes_public, "get_sources_view", fake_view)


def test_landing_renders_facts_and_example(monkeypatch):
    from datetime import datetime, timezone

    from src.api.public import geo_cache

    async def fake_facts(_db):
        return {
            "open_total": 1696,
            "updated_at": datetime.now(timezone.utc),
            "example": _example_row(),
        }

    monkeypatch.setattr(geo_cache, "get_landing_facts", fake_facts)
    _fake_sources(monkeypatch, [("Portal javnih nabavki", ("RS",))])
    html = _client().get("/?lang=sr").text
    assert "Otvorenih tendera sada: 1696" in html
    assert "ažurirano danas" in html
    assert "izvori: Portal javnih nabavki" not in html  # the list of names is removed
    assert "Primer: najnoviji otvoreni tender" in html
    assert 'href="/tender/5d77eebf-5531-4645-826a-f7378bf80950"' in html
    assert "Nabavka kancelarijskog materijala" in html


def test_landing_survives_db_error(monkeypatch):
    from src.api.public import geo_cache

    async def boom(_db):
        raise RuntimeError("db down")

    monkeypatch.setattr(geo_cache, "get_landing_facts", boom)
    resp = _client().get("/?lang=sr")
    assert resp.status_code == 200
    assert "data-landing-facts" not in resp.text
    assert "Javne nabavke u Srbiji" in resp.text


def test_landing_facts_reuse_tenderi_counters(monkeypatch):
    """open_total = the sum of ``open`` over the CPV sections of the /tenderi hub, cached."""
    import asyncio

    from src.api import repo
    from src.api.public import geo_cache

    geo_cache.clear_cache()
    calls = {"cpv": 0}

    async def fake_cpv(_db):
        calls["cpv"] += 1
        return {
            "45": {"total": 10, "open": 7, "open_sum_eur": None, "label": "x"},
            "30": {"total": 9, "open": 3, "open_sum_eur": None, "label": "y"},
        }

    async def fake_changed(_db):
        return None

    async def fake_example(_db):
        return None

    async def fake_enabled(_db):
        return []

    monkeypatch.setattr(repo, "cpv_division_stats", fake_cpv)
    monkeypatch.setattr(repo, "latest_tender_change_at", fake_changed)
    monkeypatch.setattr(repo, "latest_open_example", fake_example)
    monkeypatch.setattr(geo_cache, "list_enabled_sources", fake_enabled)
    try:
        facts = asyncio.run(geo_cache.get_landing_facts(object()))
        again = asyncio.run(geo_cache.get_landing_facts(object()))
    finally:
        geo_cache.clear_cache()
    assert facts["open_total"] == 10
    assert again is facts and calls["cpv"] == 1


# --------------------------------------------------------------------------- #
# Step 7: brand, title, og:image, sameAs, indexing ≤90 days
# --------------------------------------------------------------------------- #
import re  # noqa: E402


def _title(html: str) -> str:
    return re.search(r"<title>(.*?)</title>", html, re.S).group(1).strip()


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("/login?lang=sr", "Prijava — TenderSRB"),
        ("/signup?lang=sr", "Registracija — TenderSRB"),
        ("/login?lang=ru", "Вход — TenderSRB"),
        ("/signup?lang=en", "Sign up — TenderSRB"),
        ("/plans?lang=en", "Pricing — TenderSRB"),
    ],
)
def test_titles_carry_brand(path, expected):
    html = _client().get(path).text
    assert _title(html) == expected
    assert "Tender Bot Serbia" not in html


def test_old_brand_gone_outside_legal_texts():
    from pathlib import Path

    src = Path(__file__).resolve().parents[2] / "src"
    hits = [
        str(p.relative_to(src))
        for p in [*src.rglob("*.py"), *src.rglob("*.html")]
        if "Tender Bot Serbia" in p.read_text(encoding="utf-8")
    ]
    # Legal texts — the owner’s decision (we do not touch them); the rest — docstrings of the packages.
    allowed = {
        "api/public/legal_texts.py",
        "__init__.py",
        "db/__init__.py",
        "db/base.py",
        "db/models/__init__.py",
        "workers/__init__.py",
    }
    assert set(hits) <= allowed, sorted(set(hits) - allowed)


@pytest.mark.parametrize("path", ["/", "/plans", "/help", "/tenderi"])
def test_public_pages_have_large_og_image(path, monkeypatch):
    from src.api.public import geo_cache

    async def no_stats(_db):
        return {}

    monkeypatch.setattr(geo_cache, "get_cpv_stats", no_stats)
    monkeypatch.setattr(geo_cache, "get_region_stats", no_stats)
    html = _client().get(path).text
    m = re.search(r'<meta property="og:image" content="([^"]+)">', html)
    assert (
        m
        and m.group(1).startswith("http")
        and "/static/og/default.png?v=" in m.group(1)
    )
    assert '<meta name="twitter:card" content="summary_large_image">' in html


def test_og_image_asset_is_1200x630_png():
    import struct
    from pathlib import Path

    png = (
        Path(__file__).resolve().parents[2] / "src/api/static/og/default.png"
    ).read_bytes()
    assert png[:8] == b"\x89PNG\r\n\x1a\n"
    assert struct.unpack(">II", png[16:24]) == (1200, 630)


def test_landing_organization_same_as_telegram_bot():
    html = _client().get("/?lang=sr").text
    assert '"sameAs": ["https://t.me/nabrs_bot"]' in html or (
        '"sameAs":["https://t.me/nabrs_bot"]' in html
    )


def test_indexable_boundary_90_days():
    from datetime import datetime, timedelta, timezone

    from src.domain.indexing import INDEXABLE_SQL, is_indexable

    now = datetime(2026, 9, 30, 12, tzinfo=timezone.utc)
    day = timedelta(days=1)
    sec = timedelta(seconds=1)
    assert is_indexable(now + day, None, now)  # open
    assert is_indexable(now - 90 * day + sec, None, now)  # closed 90d without a second
    assert not is_indexable(now - 90 * day, None, now)  # exactly 90d — outside (strictly >)
    assert not is_indexable(now - 91 * day, now, now)  # the deadline decides, not published
    assert is_indexable(None, now - 10 * day, now)  # without a deadline — by publication
    assert not is_indexable(None, None, now)  # without dates — the freshness is not proven
    # SQL mirror: the same boundary and the same reference point.
    assert INDEXABLE_SQL == (
        "COALESCE(deadline_at, published_at) > now() - interval '90 days'"
    )


def test_sitemap_queries_use_indexable_filter():
    import asyncio

    from src.api import repo
    from tests.api.conftest import FakeResult, FakeSession

    class _Capture(FakeSession):
        async def execute(self, stmt, params=None):
            self.sqls.append(str(stmt))
            return FakeResult(items=[])

    s = _Capture()
    s.sqls = []
    asyncio.run(repo.list_tenders_for_sitemap(s, 0, 10))
    assert "COALESCE(deadline_at, published_at) > now() - interval '30 days'" in (
        s.sqls[0]
    )


def test_sitemap_window_30_days():
    """Sitemap — a 30-day window; the card (noindex) — still 90 (see below)."""
    from datetime import datetime, timedelta, timezone

    from src.domain.indexing import SITEMAP_SQL, is_sitemap_indexable

    now = datetime(2026, 10, 14, 12, tzinfo=timezone.utc)
    day = timedelta(days=1)
    assert is_sitemap_indexable(now + day, None, now)  # open
    assert is_sitemap_indexable(now - 29 * day, None, now)  # closed 29d — in the sitemap
    assert not is_sitemap_indexable(now - 31 * day, None, now)  # closed 31d — not
    assert is_sitemap_indexable(None, now - 29 * day, now)  # without a deadline — by publication
    assert not is_sitemap_indexable(None, None, now)  # without dates — no
    # SQL mirror: the same 30-day window, the same reference point.
    assert SITEMAP_SQL == (
        "COALESCE(deadline_at, published_at) > now() - interval '30 days'"
    )


def _tender_row(**kw):
    from datetime import datetime, timedelta, timezone
    from types import SimpleNamespace

    base = dict(
        id="11111111-1111-4111-8111-111111111111",
        title="Nabavka",
        customer_name=None,
        amount_eur=None,
        is_amount_estimated=False,
        deadline_at=datetime.now(timezone.utc) + timedelta(days=20),
        published_at=None,
        status="active",
        regions=[],
        procedure_type=None,
        description=None,
        classifications=None,
        source_url=None,
        canonical_tender_id=None,
    )
    base.update(kw)
    return SimpleNamespace(**base)


@pytest.mark.parametrize(
    ("days_ago", "noindex"),
    [(-20, False), (30, False), (31, False), (91, True), (120, True)],
)
def test_tender_page_noindex_for_old_closed(days_ago, noindex):
    from datetime import datetime, timedelta, timezone

    from tests.api.conftest import FakeResult, FakeSession

    row = _tender_row(deadline_at=datetime.now(timezone.utc) - timedelta(days=days_ago))
    session = FakeSession(results=[FakeResult(items=[row])])
    app.dependency_overrides[deps.get_session_factory] = lambda: (lambda: session)
    app.dependency_overrides[deps.get_optional_session] = lambda: None
    html = TestClient(app).get("/tender/11111111-1111-4111-8111-111111111111").text
    assert ('<meta name="robots" content="noindex,follow">' in html) is noindex


def test_plans_page_shows_ai_limit_numbers_not_checks():
    """The comparison table carries the limit numbers (Pro/Business), as in _TIER_LIMITS.

    The expectation — a literal from the fact of 07.10.2026. Mutation: remove the substitution in ``showcase_feature_matrix`` → “400 / mo” disappears.
    """
    from src.ai.usage import _TIER_LIMITS

    html = _client().get("/plans?lang=en").text
    rows = {
        "AI matching with relevance explanation": ("400", "1500"),
        "Deep AI specification analysis": ("10", "50"),
        "Bid / no-bid verdict": ("30", "150"),
        "Application document checklist": ("30", "150"),
        "Bid draft": ("5", "30"),
    }
    for label, (pro, biz) in rows.items():
        m = re.search(
            rf'<th scope="row"[^>]*>\s*{re.escape(label)}\s*</th>(.*?)</tr>', html, re.S
        )
        assert m, label
        cells = re.findall(r"<td[^>]*>(.*?)</td>", m.group(1), re.S)
        text = [re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", c)).strip() for c in cells]
        assert text == [f"{pro} / mo", f"{biz} / mo"], (label, text)
    assert _TIER_LIMITS["pro"]["standard"] == 400  # the literal above = the code today
