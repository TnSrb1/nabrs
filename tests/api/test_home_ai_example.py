"""Home: the block “Primer: šta dobijate uz tender” — a real snapshot of the AI analysis.

Expectations — literals from production data (tender_ai_analyses a089c9c7…, 23.07.2026).
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from src.api import deps
from src.api.main import app
from tests.api.conftest import FakeSession


def _get(lang: str) -> str:
    app.dependency_overrides[deps.get_session_factory] = lambda: FakeSession
    app.dependency_overrides[deps.get_optional_session] = lambda: None
    try:
        resp = TestClient(app, follow_redirects=False).get("/", cookies={"lang": lang})
    finally:
        app.dependency_overrides.clear()
    assert resp.status_code == 200
    return resp.text


COMMON = [
    "Nabavka IT opreme za obradu i skladištenje podataka",
    "Republički zavod za zaštitu spomenika kulture",
    "320.000 RSD",
    "23.07.2026",
    "/tender/ede75f22-15c0-4cf2-8d57-354b29a7f5a8",
]


@pytest.mark.parametrize(
    "lang,caption,verdict,button,note,pro,con",
    [
        (
            "sr",
            "Primer AI analize od 23.07.2026",
            "Učešće uz ograde",
            "u ličnom kabinetu, na tarifi Pro i višoj",
            "Primer, skraćeno",
            "Otvoreni postupak (Open tender)",
            "Zahtev za finansijsko obezbeđenje (blanko menica 10% od vrednosti)",
        ),
        (
            "en",
            "Example AI analysis from 23.07.2026",
            "Participate with caveats",
            "in your account, on the Pro plan and above",
            "Example, abridged",
            "Open procedure (Open tender)",
            "Financial security requirement (blank promissory note, 10% of the value)",
        ),
        (
            "ru",
            "Пример AI-разбора от 23.07.2026",
            "Участвовать с оговорками",
            "в личном кабинете — на тарифе Pro и выше",
            "Текст разбора — как сгенерирован",
            "Открытая процедура (Open tender)",
            "Требование финансового обеспечения (бланко-вексель 10% от стоимости)",
        ),
    ],
)
def test_home_ai_example_rendered(lang, caption, verdict, button, note, pro, con):
    html = _get(lang)
    for lit in COMMON + [caption, verdict, button, note, pro, con]:
        assert lit in html, lit
    assert "data-home-ai-example" in html
    # The analysis block — in the language of the page (sr/en — translation, ru — source).
    block = html.split("data-home-ai-example", 1)[1].split("</section>", 1)[0]
    assert f'lang="{lang}"' in block
    if lang != "ru":
        assert "Открытая процедура" not in block


@pytest.mark.parametrize(
    "lang,disclaimer",
    [
        ("sr", "AI može da greši — proverite sa dokumentacijom nabavke."),
        ("ru", "AI может ошибаться — сверяйте с документацией закупки."),
        ("en", "AI can make mistakes — check against the procurement documentation."),
    ],
)
def test_home_ai_example_has_ai_disclaimer(lang, disclaimer):
    # #635: AI disclaimer — at all places where the analysis is shown (as tender.html:256).
    html = _get(lang)
    block = html.split("data-home-ai-example", 1)[1].split("</section>", 1)[0]
    assert disclaimer in block


def test_sr_title_and_no_cyrillic_in_headings():
    html = _get("sr")
    assert "Primer: šta dobijate uz tender" in html
    assert "Набавка ИТ опреме" not in html
    assert (
        "onclick"
        not in html.split("data-home-ai-example", 1)[1].split("</section>", 1)[0]
    )
