"""Context transitions “All tenders” ↔ “Sources” (owner 02.10.2026).

Expectations — literals of href and captions, not via ``t()``.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from src.api import deps
from src.api.main import app
from tests.api.conftest import FakeSession
from tests.api.test_izvori_page import ALL, _get


def teardown_function():
    app.dependency_overrides.clear()


def _hub(lang="ru"):
    session = FakeSession(results=[])
    app.dependency_overrides[deps.get_session_factory] = lambda: lambda: session
    app.dependency_overrides[deps.get_optional_session] = lambda: None
    return TestClient(app, follow_redirects=False).get(f"/tenderi?lang={lang}")


def test_hub_links_to_izvori_with_count():
    # conftest.autouse: 1 source enabled (jnportal)
    html = _hub("ru").text
    assert (
        '<a href="/izvori" class="src-link underline">Тендеры из источников: 1 →</a>'
        in html
    )
    assert "Tenderi iz izvora: 1 →" in _hub("sr").text
    assert "Tender sources: 1 →" in _hub("en").text


def test_izvori_card_links_to_tenders_of_that_source():
    html = _get("/izvori?lang=ru", ALL).text
    assert 'href="/tenderi?source=jnportal"' in html
    assert "Тендеры этой площадки →" in html
    # disabled sources do not get links
    assert "source=ted_balkan" not in html and "source=bih_enabavke" not in html
