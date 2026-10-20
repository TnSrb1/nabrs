"""The public showcase ``/statistika`` (R3 ANALYTICS_PRODUCT, owner 10.10.2026).

The render for an anonymous user in three languages and the line “Analitika i izveštaji (Excel)” on /plans. Expectations — LITERALS (not via the same catalog STATS/PLANS that the code uses). The aggregates on the real SQL — ``tests/db/test_public_stats_sql.py``.
"""

from __future__ import annotations

import asyncio
import dataclasses
import re
from datetime import datetime, timezone
from decimal import Decimal

from fastapi.testclient import TestClient

from src.api import deps
from src.api.main import app
from src.api.public import stats_page
from src.api.public.stats_page import (
    AwardMonth,
    CountRow,
    CustomerRow,
    PublicStats,
)
from src.plans.catalog import comparison_matrix, get_plan
from tests.api.conftest import FakeSession

STATS = PublicStats(
    period_start=datetime(2026, 6, 1, 22, 0, tzinfo=timezone.utc),
    as_of=datetime(2026, 10, 9, 20, 0, tzinfo=timezone.utc),
    tenders_total=1500,
    tender_months=(
        CountRow("06.2026", 500, 55),
        CountRow("07.2026", 900, 100),
        CountRow("10.2026", 100, 11),
    ),
    cpv_top=(CountRow("45", 700, 100), CountRow("33", 300, 43)),
    procedures=(CountRow("open", 1400, 100), CountRow("", 100, 7)),
    award_months=(
        AwardMonth("06.2026", 10, 7, Decimal("1234567.40"), 100),
        AwardMonth("07.2026", 4, 0, None, 40),
    ),
    awards_total=14,
    awards_known=7,
    awards_sum_eur=Decimal("1234567.40"),
    top_customers=(CustomerRow("Grad Beograd", 3, Decimal("900000")),),
    current_month="10.2026",
)


def teardown_function():
    app.dependency_overrides.clear()
    stats_page.clear_cache()


def _client(monkeypatch, stats=STATS) -> TestClient:
    async def fake_stats(_factory):
        return stats

    monkeypatch.setattr(stats_page, "get_public_stats", fake_stats)
    app.dependency_overrides[deps.get_session_factory] = lambda: FakeSession
    app.dependency_overrides[deps.get_optional_session] = lambda: None
    return TestClient(app, follow_redirects=False)


def test_statistika_sr_default_renders_aggregates(monkeypatch):
    r = _client(monkeypatch).get("/statistika")
    assert r.status_code == 200
    html = r.text
    assert "Statistika javnih nabavki u Srbiji" in html
    # The dates — from the data (Belgrade), not a literal in the template.
    assert "Podaci od 02.06.2026 · stanje na 09.10.2026" in html
    assert "Tendera u periodu: 1500" in html
    assert "45 · Građevinski radovi" in html  # sr — Latin, not the Cyrillic of the directory
    assert "Otvoreni postupak" in html
    assert "Nije navedeno" in html
    assert "€1\u00a0234\u00a0567" in html  # the digit groups are non-breaking (the number does not break)
    assert "iznos poznat za 7 od 14" in html
    assert "iznos poznat za 0 od 4" in html
    assert "Grad Beograd" in html
    assert "Sastavite sopstveni izveštaj i preuzmite Excel — 30 dana besplatno" in html
    assert 'href="/signup"' in html
    assert (
        "{"
        not in re.sub(r"<script.*?</script>", "", html, flags=re.S)
        .split("<main", 1)[1]
        .split("</main>", 1)[0]
    )


def test_statistika_cpv_34_renders_latin_sr_label(monkeypatch):
    """Section 34 in the sr showcase — in Latin; a literal, not via cyr_to_lat."""
    stats = dataclasses.replace(STATS, cpv_top=(CountRow("34", 50, 100),))
    html = _client(monkeypatch, stats=stats).get("/statistika").text
    assert "34 · Oprema i pomoćni proizvodi za prevoz" in html


def test_statistika_en_and_ru(monkeypatch):
    client = _client(monkeypatch)
    en = client.get("/statistika?lang=en")
    assert en.status_code == 200
    assert "Data since 02.06.2026 · as of 09.10.2026" in en.text
    assert "45 · Construction work" in en.text
    assert "amount known for 7 of 14" in en.text
    assert "Build your own report and download Excel — 30 days free" in en.text
    ru = client.get("/statistika?lang=ru")
    assert ru.status_code == 200
    assert "Данные с 02.06.2026 · по состоянию на 09.10.2026" in ru.text
    assert "45 · Строительные работы" in ru.text
    assert "сумма известна для 7 из 14" in ru.text


def test_statistika_never_shows_winners_or_pib(monkeypatch):
    html = _client(monkeypatch).get("/statistika").text
    low = html.lower()
    assert "pib" not in low and "пиб" not in low
    assert "pobednik" not in low


def test_statistika_empty_state_when_no_data(monkeypatch):
    r = _client(monkeypatch, stats=None).get("/statistika")
    assert r.status_code == 200
    assert "Statistika trenutno nije dostupna. Pokušajte kasnije." in r.text
    assert "Podaci od" not in r.text
    # The CTA stays even without figures.
    assert 'href="/signup"' in r.text


def test_statistika_head_and_sitemap(monkeypatch):
    client = _client(monkeypatch)
    assert client.head("/statistika").status_code == 200
    assert "/statistika" in client.get("/sitemap-pages.xml").text
    assert 'href="/statistika"' in client.get("/statistika").text  # footer


def test_get_public_stats_failure_is_cached_briefly(monkeypatch):
    """A DB failure → None and a short cache: the second call does not go to the DB."""
    calls = {"n": 0}

    class Boom:
        async def __aenter__(self):
            calls["n"] += 1
            raise RuntimeError("db down")

        async def __aexit__(self, *a):
            return False

    stats_page.clear_cache()
    assert asyncio.run(stats_page.get_public_stats(Boom)) is None
    assert asyncio.run(stats_page.get_public_stats(Boom)) is None
    assert calls["n"] == 1


def test_fmt_eur_none_is_dash_not_zero():
    assert stats_page.fmt_eur(None) == "—"
    assert stats_page.fmt_eur(Decimal("0")) == "€0"
    # the digit groups are non-breaking (U+00A0): the number does not break across lines in a table cell
    assert stats_page.fmt_eur(Decimal("276507123")) == "€276\u00a0507\u00a0123"


# --- /plans: the line “Analitika i izveštaji (Excel)” -------------------------


def test_reports_flag_business_enterprise_only():
    assert get_plan("business").reports is True
    assert get_plan("enterprise").reports is True
    assert get_plan("pro").reports is False
    assert get_plan("free").reports is False
    row = next(r for r in comparison_matrix() if r["key"] == "reports")
    assert [c["on"] for c in row["cells"]] == [False, True]  # Pro / Business


def _card(html: str, plan_name: str) -> str:
    """The HTML of the plan card — from the heading of the plan to the next card."""
    start = html.index(f">{plan_name}<")
    nxt = html.find('<div class="relative rounded-lg border', start)
    return html[start : nxt if nxt != -1 else len(html)]


def test_plans_page_reports_row_business_on_pro_off(monkeypatch):
    html = _client(monkeypatch).get("/plans?lang=sr").text
    label = "Analitika i izveštaji (Excel)"
    assert label in html
    row = re.search(
        r"<th scope=\"row\"[^>]*>Analitika i izveštaji \(Excel\)</th>(.*?)</tr>",
        html,
        flags=re.S,
    )
    assert row is not None
    cells = re.findall(r"feature-(yes|no)", row.group(1))
    assert cells == ["no", "yes"]  # Pro — no, Business — yes
    en = _client(monkeypatch).get("/plans?lang=en").text
    assert "Analytics &amp; reports (Excel)" in en


def test_stats_catalog_key_parity_and_sr_latin():
    from src.api.public.i18n import STATS, UI

    assert set(STATS["sr"]) == set(STATS["ru"]) == set(STATS["en"])
    assert all("nav_stats" in UI[lang] for lang in ("sr", "ru", "en"))
    # sr — Latin: not a single Cyrillic letter in the lines of the showcase.
    assert not re.search(r"[А-Яа-яЁёЂђЈјЉљЊњЋћЏџ]", " ".join(STATS["sr"].values()))


def test_statistika_marks_current_month_and_lag_note_in_all_locales(monkeypatch):
    client = _client(monkeypatch)
    expect = {
        "sr": ("10.2026 (tekući mesec, nepotpuno)", "poslednja dva meseca"),
        "ru": ("10.2026 (текущий месяц, неполный)", "за последние два месяца"),
        "en": ("10.2026 (current month, incomplete)", "for the last two months"),
    }
    for lang, (partial, lag) in expect.items():
        html = client.get(f"/statistika?lang={lang}").text
        # The mark both in the row of tenders and in the row of decisions (in the decision rows of the fixture — 06, 07:
        # the mark is there only for the matching month, i.e. for the row of tenders).
        assert partial in html, lang
        assert lag in html, lang
        assert "07.2026 (" not in html, lang
    sr = client.get("/statistika?lang=sr").text
    assert "Odluka se računa po partiji" not in sr
    assert "jedan ponuđač u jednom tenderu" in sr


# CPV sections without a Serbian name (`l_sr`) in src/api/static/cpv_codes.json.
# cpv-sr-labels (14.10.2026): the captions are taken from the portal (34, 35, 43, 90) and
# translated by the pattern of the directory (14, 19, 41, 51, 70, 75, 76, 79) — there are no gaps.
# The appearance of a gap again = a regression: the test falls, rather than quietly going to English.
_CPV_DIVISIONS_WITHOUT_SR: set[str] = set()


def test_cpv_division_names_sr_are_serbian_where_source_has_translation():
    import json
    from pathlib import Path

    from src.api.public.routes_public import _cpv_division_name

    rows = json.loads(Path("src/api/static/cpv_codes.json").read_text("utf-8"))
    divisions = [r for r in rows if r["c"].endswith("000000")]
    assert len(divisions) == 45
    missing = set()
    for r in divisions:
        code = r["c"][:2]
        name = _cpv_division_name(code, "sr")
        if r.get("l_sr"):
            assert name != r["l"], code  # not an English fallback
            assert not re.search(r"[Ѐ-ӿ]", name), code  # Latin
        else:
            missing.add(code)
    assert missing == _CPV_DIVISIONS_WITHOUT_SR
    assert _cpv_division_name("45", "sr") == "Građevinski radovi"
