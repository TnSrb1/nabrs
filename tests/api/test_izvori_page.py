"""The sources showcase ``GET /izvori`` (owner decision 02.10.2026).

Expectations — literals (captions and production facts of 02.10), not via the same
catalog the page renders.

The fake table ``sources`` stores both enabled and disabled rows and
applies the ``enabled`` filter ONLY if it is in the page SQL: remove
``WHERE s.enabled = true`` from the query — a disabled source will slip onto the showcase
and the test goes red.
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from src.api import deps
from src.api.main import app
from src.api.public import sources_page
from tests.api.conftest import FakeResult, FakeSession


def _row(key, name, codes, base_url, interval, enabled, since=None):
    return SimpleNamespace(
        source_key=key,
        display_name=name,
        country_codes=codes,
        base_url=base_url,
        polling_interval_sec=interval,
        collecting_since=since,
        enabled=enabled,
    )


# Production fact 02.10.2026 (SELECT … FROM sources): only jnportal is enabled.
JNPORTAL = _row(
    "jnportal",
    "Portal javnih nabavki (UJN)",
    ["RS"],
    "https://jnportal.src-ujn.example.invalid",
    1800,
    True,
    since=datetime(2026, 6, 3, 9, 18, tzinfo=timezone.utc),
)
TED_BALKAN_OFF = _row(
    "ted_balkan",
    "TED — Zapadni Balkan",
    ["BA", "ME", "MK"],
    "https://api.src-ted.example.invalid",
    10800,
    False,
)
BIH_OFF = _row(
    "bih_enabavke",
    "e-Nabavke BiH",
    ["BA"],
    "https://open.src-ejn.example.invalid",
    3600,
    False,
)


class SourcesTableSession(FakeSession):
    def __init__(self, rows, tender_counts=()):
        super().__init__()
        self._rows = rows
        self._tender_counts = tender_counts

    async def execute(self, statement, params=None):
        sql = str(statement)
        if "collecting_since" in sql and "FROM sources" in sql:
            rows = self._rows
            if "s.enabled = true" in sql:
                rows = [r for r in rows if r.enabled]
            return FakeResult(items=list(rows))
        if "FROM tenders_normalized" in sql:
            return FakeResult(
                items=[
                    SimpleNamespace(source_key=k, country_code=c, n=n)
                    for k, c, n in self._tender_counts
                ]
            )
        return await super().execute(statement, params)


def teardown_function():
    app.dependency_overrides.clear()


def _get(path, rows, tender_counts=()):
    session = SourcesTableSession(rows, tender_counts)
    app.dependency_overrides[deps.get_session_factory] = lambda: lambda: session
    app.dependency_overrides[deps.get_optional_session] = lambda: None
    return TestClient(app, follow_redirects=False).get(path)


ALL = [BIH_OFF, JNPORTAL, TED_BALKAN_OFF]


@pytest.mark.parametrize("lang", ["sr", "ru", "en"])
def test_izvori_200_on_every_language(lang):
    resp = _get(f"/izvori?lang={lang}", ALL)
    assert resp.status_code == 200
    assert f'<html lang="{lang}"' in resp.text


def test_enabled_source_present_with_anchor_and_facts():
    html = _get("/izvori?lang=sr", ALL).text
    assert '<article id="jnportal" class="card">' in html
    assert "Portal javnih nabavki (UJN)" in html
    assert "Zemlje: Srbija" in html
    assert 'href="https://jnportal.src-ujn.example.invalid"' in html
    assert "Nove objave proveravamo svakih 30 min" in html
    # “Collecting from” — the first tender (03.06), not enabled_at of migration 0045.
    assert "Tenderi iz ovog izvora od 03.06.2026" in html


def test_disabled_sources_absent_from_html():
    html = _get("/izvori?lang=sr", ALL).text
    for needle in (
        "ted_balkan",
        "TED — Zapadni Balkan",
        "bih_enabavke",
        "e-Nabavke BiH",
    ):
        assert needle not in html


def test_jnportal_matrix_is_honest_literals():
    html = _get("/izvori?lang=ru", ALL).text
    assert "Срок подачи заявок" in html
    # The amount for jnportal is present for 65% of tenders — “if stated”, not unconditionally.
    assert "Оценочная стоимость — если указана заказчиком" in html
    assert "Текст конкурсной документации — AI-разбор читает саму документацию" in html
    assert "Уведомление о выбранном победителе" in html
    assert "Язык объявлений: сербский, кириллица" in html
    # There is no description in a jnportal notice — the row is shown with a dash.
    assert (
        'feature-row feature-off">'
        in html.split("Описание предмета из самого объявления")[0][-400:]
    )


def test_dedup_row_only_when_partner_enabled():
    off = _get("/izvori?lang=en", ALL).text
    assert "marked as a duplicate" not in off
    sources_page.clear_cache()
    ted_on = SimpleNamespace(**{**vars(TED_BALKAN_OFF), "enabled": True})
    on = _get("/izvori?lang=en", [JNPORTAL, ted_on]).text
    assert (
        "The same tender also published on: TED — Zapadni Balkan — marked as a duplicate"
        in on
    )
    # The TED site for a person, not the API address from sources.base_url.
    assert 'href="https://src-ted.example.invalid"' in on
    assert "api.src-ted.example.invalid" not in on


def test_empty_list_renders_honest_empty_state():
    html = _get("/izvori?lang=sr", [BIH_OFF, TED_BALKAN_OFF]).text
    assert "Trenutno nijedan izvor nije uključen." in html
    assert "<article" not in html


def test_seo_canonical_and_hreflang():
    html = _get("/izvori?lang=en", ALL).text
    assert '<link rel="canonical" href="' in html
    assert 'hreflang="sr"' in html and 'hreflang="en"' in html


def test_footer_plans_help_link_to_izvori():
    assert 'href="/izvori"' in _get("/izvori?lang=sr", ALL).text
    for path in ("/plans?lang=sr", "/help?lang=sr"):
        sources_page.clear_cache()
        html = _get(path, ALL).text
        assert html.count('href="/izvori"') >= 2, path  # footer + the page row


# SRC-0c (05.10.2026): the mod/ukck rows — as the ingest autoseed will create them
# (``display_name`` = the adapter’s ``source_name``, the adapter’s ``base_url``, disabled).
MOD_OFF = _row(
    "mod_nabavke",
    "Ministarstvo odbrane — nabavke ispod praga",
    ["RS"],
    "http://www.nabavke.src-mod.example.invalid",
    3600,
    False,
)
UKCK_OFF = _row(
    "ukck_nabavke",
    "UKC Kragujevac — nabavke ispod praga",
    ["RS"],
    "https://src-ukck.example.invalid",
    21600,
    False,
)


def _on(row):
    return SimpleNamespace(**{**vars(row), "enabled": True})


def _is_off(html: str, label: str) -> bool:
    """The matrix row with the ``label`` caption is shown with a dash."""
    return 'feature-row feature-off">' in html.split(label)[0][-400:]


def test_seeded_mod_ukck_rows_are_not_on_the_page():
    sources_page.clear_cache()
    html = _get("/izvori?lang=sr", [JNPORTAL, MOD_OFF, UKCK_OFF]).text
    for needle in (
        "mod_nabavke",
        "Ministarstvo odbrane",
        "ukck_nabavke",
        "UKC Kragujevac",
    ):
        assert needle not in html


@pytest.mark.parametrize(
    ("lang", "deadline", "deadline_if_stated", "link"),
    [
        (
            "sr",
            "Rok za podnošenje ponuda",
            "Rok za podnošenje ponuda — ako je čitljiv u pozivu",
            "Link na objavu na sajtu izvora",
        ),
        (
            "ru",
            "Срок подачи заявок",
            "Срок подачи заявок — если читается в приглашении",
            "Ссылка на объявление на сайте площадки",
        ),
        (
            "en",
            "Bid submission deadline",
            "Bid submission deadline — if readable in the invitation",
            "Link to the notice on the source website",
        ),
    ],
)
def test_enabled_mod_ukck_have_label_and_honest_matrix(
    lang, deadline, deadline_if_stated, link
):
    sources_page.clear_cache()
    html = _get(f"/izvori?lang={lang}", [JNPORTAL, _on(MOD_OFF), _on(UKCK_OFF)]).text

    mod = html.split('<article id="mod_nabavke" class="card">')[1].split("</article>")[
        0
    ]
    ukck = html.split('<article id="ukck_nabavke" class="card">')[1].split(
        "</article>"
    )[0]
    assert "Ministarstvo odbrane — nabavke ispod praga" in mod
    assert "UKC Kragujevac — nabavke ispod praga" in ukck
    # The deadline at MO — a dash (PDF behind bot protection); at UKC — “if readable”
    # (the text of the PDF “Poziv”, the scans have no deadline, owner 06.10.2026), not unconditionally.
    assert _is_off(mod, deadline)
    assert deadline_if_stated in ukck and not _is_off(ukck, deadline_if_stated)
    # Direct link: for MO — the jn.php card, for UKC — only the general page.
    assert not _is_off(mod, link)
    assert _is_off(ukck, link)
    # A site for a person — the UKC procurement page, not the root/WP REST.
    assert 'href="https://src-ukck.example.invalid/javne-nabavke/"' in ukck


# The order of the showcase (owner 16.10.2026): main → Serbia (RS) → other countries;
# within a block — the number of tenders DESC, on a tie — source_key. The fixture
# is arranged so that the alphabet and the number give a different order: jnportal with the
# smallest number (5) is still first. ted_balkan — a multi-country one with RS (fact of
# production 16.10: BA/ME/MK/RS), therefore in the Serbia block: 61 > nis 59.
@pytest.mark.asyncio
async def test_order_primary_then_serbia_then_countries_by_count():
    sources_page.clear_cache()
    nis = _row("nis", "NIS", ["RS"], "https://nis.example", 3600, True)
    cejn = _row("cejn", "CEJN", ["RS"], "https://cejn.example", 3600, True)
    eeas = _row("eeas", "EEAS", ["RS", "BA", "ME"], "https://eeas.example", 3600, True)
    rows = [
        _row("bih_enabavke", "e-Nabavke BiH", ["BA"], "https://x", 3600, True),
        _row(
            "ted_balkan",
            "TED",
            ["RS", "BA", "ME", "MK"],
            "https://y",
            3600,
            True,
        ),
        _on(MOD_OFF),
        JNPORTAL,
        nis,
        cejn,
        eeas,
    ]
    counts = [
        ("jnportal", "RS", 5),
        ("nis", "RS", 59),
        ("cejn", "RS", 44),
        ("ted_balkan", "ME", 40),
        ("ted_balkan", "BA", 21),
        ("bih_enabavke", "BA", 90),
    ]
    session = SourcesTableSession(rows, counts)
    cards = await sources_page.get_source_cards(session)
    # eeas and mod_nabavke — zero: a tie → source_key (“eeas” < “mod_nabavke”).
    # A non-RS country — bih (BA, sum 90) alone: the BA block goes after the Serbia block.
    assert [c.key for c in cards] == [
        "jnportal",
        "ted_balkan",
        "nis",
        "cejn",
        "eeas",
        "mod_nabavke",
        "bih_enabavke",
    ]


def test_home_country_multi_without_serbia_takes_largest_share():
    # RS among the countries — the Serbia block; without RS — the country with the most tenders
    # of a source; on a tie and zero — the first alphabetically.
    home = sources_page._home_country
    assert home(("RS", "BA"), {"BA": 99}) == "RS"
    assert home(("BA", "ME", "MK"), {"BA": 21, "ME": 40}) == "ME"
    assert home(("MK", "BA"), {}) == "BA"
    assert home((), {}) == ""
