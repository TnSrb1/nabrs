"""GEO/SEO foundation: robots.txt, sitemap.xml (+chunks), llms.txt, favicon.

Before the task geo_base all four paths were 404 (see TESTING_geo_base.md — a fact of the live production 26.09.2026). The DB is mocked via dependency_overrides (ADR-013).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from xml.etree import ElementTree as ET

from fastapi.testclient import TestClient

from src.api import deps
from src.api.main import app
from tests.api.conftest import FakeResult, FakeSession


def _client(session: FakeSession | None = None) -> TestClient:
    session = session or FakeSession()

    def factory():
        return session

    app.dependency_overrides[deps.get_session_factory] = lambda: factory
    app.dependency_overrides[deps.get_optional_session] = lambda: None
    return TestClient(app, follow_redirects=False)


def teardown_function():
    app.dependency_overrides.clear()


# --------------------------------------------------------------------------- #
# robots.txt
# --------------------------------------------------------------------------- #


def test_robots_txt_disallows_private_and_lists_sitemap():
    resp = _client().get("/robots.txt")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/plain")
    assert "Disallow: /cabinet" in resp.text
    assert "Disallow: /admin" in resp.text
    assert "Disallow: /billing" in resp.text
    assert "Disallow: /login" in resp.text
    assert "Disallow: /signup" in resp.text
    assert "Disallow: /plans/upgrade" in resp.text
    assert "Sitemap: https://nab.rs/sitemap.xml" in resp.text


def test_robots_txt_disallows_api_docs():
    """Mutation: remove /docs|/redoc|/openapi.json from _EXPLICIT_DISALLOW → red."""
    lines = _client().get("/robots.txt").text.splitlines()
    for path in ("/docs", "/redoc", "/openapi.json"):
        assert f"Disallow: {path}" in lines


def test_robots_txt_does_not_block_public_tender_or_landing():
    resp = _client().get("/robots.txt")
    lines = [ln for ln in resp.text.splitlines() if ln.startswith("Disallow:")]
    assert not any(ln.strip() in ("Disallow: /", "Disallow: /tender") for ln in lines)


# --------------------------------------------------------------------------- #
# sitemap.xml + chunks
# --------------------------------------------------------------------------- #


def test_sitemap_index_is_valid_xml_with_pages_and_one_chunk_for_small_total():
    session = FakeSession(results=[FakeResult(scalar=5)])
    resp = _client(session).get("/sitemap.xml")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("application/xml")
    root = ET.fromstring(resp.text)
    locs = [
        el.text for el in root.iter("{http://www.sitemaps.org/schemas/sitemap/0.9}loc")
    ]
    assert "https://nab.rs/sitemap-pages.xml" in locs
    assert "https://nab.rs/sitemap-tenders-0.xml" in locs
    assert "https://nab.rs/sitemap-tenders-1.xml" not in locs


def test_sitemap_index_two_chunks_for_55128_like_prod():
    """55128 rows (production fact 26.09.2026) → 2 chunks (50000 + 5128)."""
    session = FakeSession(results=[FakeResult(scalar=55128)])
    resp = _client(session).get("/sitemap.xml")
    root = ET.fromstring(resp.text)
    locs = [
        el.text for el in root.iter("{http://www.sitemaps.org/schemas/sitemap/0.9}loc")
    ]
    assert "https://nab.rs/sitemap-tenders-0.xml" in locs
    assert "https://nab.rs/sitemap-tenders-1.xml" in locs
    assert "https://nab.rs/sitemap-tenders-2.xml" not in locs


def test_sitemap_pages_valid_xml_three_languages():
    resp = _client().get("/sitemap-pages.xml")
    assert resp.status_code == 200
    root = ET.fromstring(resp.text)
    locs = [
        el.text for el in root.iter("{http://www.sitemaps.org/schemas/sitemap/0.9}loc")
    ]
    assert "https://nab.rs/" in locs
    assert "https://nab.rs/?lang=ru" in locs
    assert "https://nab.rs/?lang=en" in locs
    assert "https://nab.rs/legal/privacy" in locs


def test_sitemap_tenders_chunk_url_count_and_lastmod():
    rows = [
        (
            "11111111-1111-1111-1111-111111111111",
            datetime(2026, 6, 1, tzinfo=timezone.utc),
        ),
        ("22222222-2222-2222-2222-222222222222", None),
    ]
    session = FakeSession(results=[FakeResult(scalar=2), FakeResult(items=rows)])
    resp = _client(session).get("/sitemap-tenders-0.xml")
    assert resp.status_code == 200
    root = ET.fromstring(resp.text)
    ns = "{http://www.sitemaps.org/schemas/sitemap/0.9}"
    urls = root.findall(f"{ns}url")
    assert len(urls) == 2
    locs = [u.find(f"{ns}loc").text for u in urls]
    assert "https://nab.rs/tender/11111111-1111-1111-1111-111111111111" in locs
    lastmods = [u.find(f"{ns}lastmod") for u in urls]
    assert lastmods[0].text == "2026-06-01T00:00:00Z"
    assert lastmods[1] is None  # without last_changed_at — we do not draw the lastmod


def test_sitemap_tenders_chunk_never_exceeds_protocol_limit():
    """The number of URLs in a chunk ≤ 50000 (the protocol limit of sitemaps.org): the request is limited by TENDER_CHUNK_SIZE, the test pins the value of the constant itself."""
    from src.api.public.routes_seo import TENDER_CHUNK_SIZE

    assert TENDER_CHUNK_SIZE <= 50_000


def test_sitemap_tenders_out_of_range_chunk_404():
    session = FakeSession(results=[FakeResult(scalar=5)])
    resp = _client(session).get("/sitemap-tenders-3.xml")
    assert resp.status_code == 404


# --------------------------------------------------------------------------- #
# llms.txt
# --------------------------------------------------------------------------- #


def test_llms_txt_markdown_with_pricing_and_links():
    resp = _client().get("/llms.txt")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/markdown")
    assert "# TenderSRB" in resp.text
    assert (
        "29 EUR/mes." in resp.text
    )  # the Pro price from the catalog, not a literal in the test
    assert "https://nab.rs/help" in resp.text
    assert "https://nab.rs/sitemap.xml" in resp.text
    assert "## English" in resp.text


# --------------------------------------------------------------------------- #
# favicon.ico
# --------------------------------------------------------------------------- #


def test_favicon_ico_redirects_to_svg():
    resp = _client().get("/favicon.ico")
    assert resp.status_code == 301
    assert resp.headers["location"] == "/static/favicon.svg"


# --------------------------------------------------------------------------- #
# security.txt (RFC 9116) — the #610 task public-copy-securitytxt
# --------------------------------------------------------------------------- #

_SECURITY_TXT_LITERAL = (
    "Contact: mailto:info@nab.rs\n"
    "Expires: 2027-10-10T12:00:00Z\n"
    "Preferred-Languages: sr, en, ru\n"
    "Canonical: https://nab.rs/.well-known/security.txt\n"
)


def test_security_txt_body_literal_expires_one_year_after_build():
    from src.api.public.routes_seo import _security_txt_body

    built = datetime(2026, 10, 10, 12, 0, tzinfo=timezone.utc)
    assert _security_txt_body("https://nab.rs", built) == _SECURITY_TXT_LITERAL


def test_security_txt_both_paths_serve_rfc9116_text():
    for path in ("/.well-known/security.txt", "/security.txt"):
        resp = _client().get(path)
        assert resp.status_code == 200, path
        assert resp.headers["content-type"].startswith("text/plain"), path
        assert "Contact: mailto:info@nab.rs" in resp.text, path
        assert "Preferred-Languages: sr, en, ru" in resp.text, path
        assert "Canonical: https://nab.rs/.well-known/security.txt" in resp.text, path
        expires = next(
            line for line in resp.text.splitlines() if line.startswith("Expires: ")
        )
        # The build date is the moment of import, so not a literal: a window of +1 year from “now”.
        when = datetime.strptime(
            expires.removeprefix("Expires: "), "%Y-%m-%dT%H:%M:%SZ"
        )
        delta = when.replace(tzinfo=timezone.utc) - datetime.now(timezone.utc)
        assert timedelta(days=364) < delta < timedelta(days=366), (path, expires)


def test_security_txt_head_returns_headers_without_body():
    resp = _client().head("/security.txt")
    assert resp.status_code == 200
    assert resp.content == b""
    assert resp.headers["content-type"].startswith("text/plain")


def test_security_txt_is_not_disallowed_in_robots():
    assert "security" not in _client().get("/robots.txt").text
