"""Navigation and small things of the showcase (TASK public-nav-small, audit 07.10.2026).

Six items: 404 with the showcase header (1), the language switcher without losing the query (2), “Kontakt” in the footer and the privacy line under the form (3), HEAD on the public GET pages (4), empty regions — noindex and outside the sitemap (5), /izvori in the showcase footer on mobile (6). DB is mocked via dependency_overrides, templates render.
"""

from __future__ import annotations

import re
import time

from fastapi.testclient import TestClient
from starlette.requests import Request

from src.api import deps
from src.api.main import app
from src.api.public import geo_cache
from tests.api.conftest import FakeSession


def _client(session: FakeSession | None = None) -> TestClient:
    session = session or FakeSession(results=[])

    def factory():
        return session

    app.dependency_overrides[deps.get_session_factory] = lambda: factory
    app.dependency_overrides[deps.get_optional_session] = lambda: None
    return TestClient(app)


def teardown_function():
    app.dependency_overrides.clear()


def _prime_region(stats: dict) -> None:
    geo_cache._region_cache = (time.monotonic() + 3600, stats)


def _footer(html: str) -> str:
    m = re.search(r"<footer.*?</footer>", html, re.S)
    assert m, "there is no <footer>"
    return m.group(0)


def _header(html: str) -> str:
    m = re.search(r"<header.*?</header>", html, re.S)
    assert m, "there is no <header>"
    return m.group(0)


# --------------------------------------------------------------------------- #
# 1. 404 for an anonymous user — the showcase header (the links “Tenders” and “Plans”)
# --------------------------------------------------------------------------- #


def test_404_anon_has_public_nav_links():
    resp = _client().get("/nepostoji-xyz", headers={"accept": "text/html"})
    assert resp.status_code == 404
    head = _header(resp.text)
    assert 'href="/tenderi"' in head
    assert 'href="/plans"' in head
    assert 'href="/izvori"' in head


# --------------------------------------------------------------------------- #
# 2. The language switcher keeps the other query parameters
# --------------------------------------------------------------------------- #


def test_lang_switch_href_keeps_other_query_params():
    from src.api.public.i18n import lang_switch_href

    scope = {
        "type": "http",
        "method": "GET",
        "path": "/tenderi",
        "query_string": b"q=skola&region=RS-00&lang=sr",
        "headers": [],
    }
    req = Request(scope)
    assert lang_switch_href(req, "ru") == "?q=skola&region=RS-00&lang=ru"
    assert lang_switch_href(req, "sr") == "?q=skola&region=RS-00&lang=sr"


def test_lang_switch_href_without_query_is_plain_lang():
    from src.api.public.i18n import lang_switch_href

    scope = {
        "type": "http",
        "method": "GET",
        "path": "/tenderi",
        "query_string": b"",
        "headers": [],
    }
    assert lang_switch_href(Request(scope), "en") == "?lang=en"


def test_tenderi_lang_switch_keeps_q_on_page():
    resp = _client().get("/tenderi?q=skola")
    assert resp.status_code == 200
    assert 'href="?q=skola&amp;lang=ru"' in resp.text


# --------------------------------------------------------------------------- #
# 3. “Kontakt” in the footer; the privacy line under the /kontakt form
# --------------------------------------------------------------------------- #


def test_footer_kontakt_on_landing_and_tenderi():
    for path in ("/", "/tenderi"):
        resp = _client().get(path)
        assert resp.status_code == 200, path
        assert 'href="/kontakt"' in _footer(resp.text), path


def test_kontakt_form_has_privacy_line():
    resp = _client().get("/kontakt")
    assert resp.status_code == 200
    m = re.search(r'<form method="post" action="/kontakt".*?</form>', resp.text, re.S)
    assert m, "there is no /kontakt form"
    # The line under the form inside <main>: the footer (with the privacy link) does not count.
    between = resp.text[m.end() : resp.text.index("</main>")]
    assert re.search(r'<p[^>]*>.*?href="/legal/privacy".*?</p>', between, re.S)


def test_404_anon_footer_has_kontakt_and_izvori():
    resp = _client().get("/nepostoji-xyz", headers={"accept": "text/html"})
    foot = _footer(resp.text)
    assert 'href="/kontakt"' in foot
    assert 'href="/izvori"' in foot


# --------------------------------------------------------------------------- #
# 4. HEAD on public GET pages — 200 without a body
# --------------------------------------------------------------------------- #


def test_head_public_pages_returns_200_without_body():
    for path in ("/", "/tenderi", "/plans"):
        client = _client()
        resp = client.head(path)
        assert resp.status_code == 200, path
        assert resp.content == b"", path
        get = client.get(path)
        assert resp.headers["content-type"] == get.headers["content-type"], path


# --------------------------------------------------------------------------- #
# 5. A region without open tenders: noindex, outside the sitemap, not in the hub
# --------------------------------------------------------------------------- #


def test_empty_region_section_is_noindex_and_not_in_sitemap():
    _prime_region({"RS-26": {"total": 10, "open": 0, "open_sum_eur": None}})
    session = FakeSession(results=[])
    client = _client(session)
    page = client.get("/tenderi/region/RS-26")
    assert page.status_code == 200
    assert '<meta name="robots" content="noindex,follow">' in page.text
    sitemap = client.get("/sitemap-pages.xml")
    assert "/tenderi/region/RS-26" not in sitemap.text


def test_region_with_open_tenders_is_indexed_and_in_sitemap():
    _prime_region({"RS-00": {"total": 10, "open": 3, "open_sum_eur": None}})
    client = _client()
    page = client.get("/tenderi/region/RS-00")
    assert 'content="noindex,follow"' not in page.text
    assert "/tenderi/region/RS-00" in client.get("/sitemap-pages.xml").text


def test_hub_hides_empty_regions():
    _prime_region(
        {
            "RS-00": {"total": 10, "open": 3, "open_sum_eur": None},
            "RS-26": {"total": 10, "open": 0, "open_sum_eur": None},
        }
    )
    html = _client().get("/tenderi").text
    assert 'href="/tenderi/region/RS-00"' in html
    assert 'href="/tenderi/region/RS-26"' not in html


# --------------------------------------------------------------------------- #
# 6. /izvori in the showcase footer — available on mobile
# --------------------------------------------------------------------------- #


def test_footer_izvori_link_is_not_hidden_on_mobile():
    html = _client().get("/").text
    m = re.search(r'<a href="/izvori"[^>]*>', _footer(html))
    assert m, "no /izvori in the footer"
    assert "nav-hide-mobile" not in m.group(0)
