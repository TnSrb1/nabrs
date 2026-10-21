"""GEO/SEO on public pages: canonical https + hreflang (4 links) +
JSON-LD (Organization/WebSite on /, SoftwareApplication+Offer on /plans,
FAQPage on /help). Before this task neither canonical, nor hreflang, nor JSON-LD
existed anywhere on the showcase (grep 'ld+json' is empty, TESTING_geo_base.md)."""

from __future__ import annotations

import json
import re

from fastapi.testclient import TestClient

from src.api import deps
from src.api.main import app
from tests.api.conftest import FakeSession


def _client() -> TestClient:
    def factory():
        return FakeSession()

    app.dependency_overrides[deps.get_session_factory] = lambda: factory
    app.dependency_overrides[deps.get_optional_session] = lambda: None
    return TestClient(app, follow_redirects=False)


def teardown_function():
    app.dependency_overrides.clear()


def _json_ld_blocks(html: str) -> list[dict]:
    return [
        json.loads(m)
        for m in re.findall(
            r'<script type="application/ld\+json">(.*?)</script>', html, re.S
        )
    ]


# --------------------------------------------------------------------------- #
# canonical + hreflang (landing/plans/help/legal)
# --------------------------------------------------------------------------- #


def test_landing_canonical_https_and_four_hreflang_links():
    resp = _client().get("/")
    assert resp.status_code == 200
    assert 'rel="canonical" href="https://nab.rs/"' in resp.text
    assert resp.text.count('rel="canonical"') == 1
    hreflangs = re.findall(r'hreflang="([^"]+)" href="([^"]+)"', resp.text)
    assert len(hreflangs) == 4
    codes = {code for code, _ in hreflangs}
    assert codes == {"sr", "ru", "en", "x-default"}


def test_landing_ru_variant_self_canonicalizes_not_sr():
    """Different content by language → each variant canonicalises ITSELF, not sr."""
    resp = _client().get("/?lang=ru")
    assert 'rel="canonical" href="https://nab.rs/?lang=ru"' in resp.text


def test_plans_canonical_and_hreflang_present():
    resp = _client().get("/plans")
    assert 'rel="canonical" href="https://nab.rs/plans"' in resp.text
    assert 'hreflang="ru" href="https://nab.rs/plans?lang=ru"' in resp.text


def test_help_canonical_and_hreflang_present():
    resp = _client().get("/help")
    assert 'rel="canonical" href="https://nab.rs/help"' in resp.text
    assert 'hreflang="en" href="https://nab.rs/help?lang=en"' in resp.text


def test_legal_canonical_present():
    resp = _client().get("/legal/privacy")
    assert 'rel="canonical" href="https://nab.rs/legal/privacy"' in resp.text


# --------------------------------------------------------------------------- #
# JSON-LD
# --------------------------------------------------------------------------- #


def test_landing_json_ld_organization_and_website():
    resp = _client().get("/")
    blocks = _json_ld_blocks(resp.text)
    types = {b["@type"] for b in blocks}
    assert types == {"Organization", "WebSite"}
    org = next(b for b in blocks if b["@type"] == "Organization")
    assert org["url"] == "https://nab.rs"
    assert re.fullmatch(
        r"https://nab\.rs/static/favicon\.svg\?v=[0-9a-f]{10}", org["logo"]
    )


def test_plans_json_ld_software_application_with_offers_from_catalog():
    resp = _client().get("/plans")
    blocks = _json_ld_blocks(resp.text)
    assert len(blocks) == 1
    app_ld = blocks[0]
    assert app_ld["@type"] == "SoftwareApplication"
    prices = {o["name"]: o["price"] for o in app_ld["offers"]}
    assert prices == {"pro": "29", "business": "79"}  # Offer free removed (owner 30.09)
    assert app_ld["description"] == "Svaki novi nalog dobija 30 dana Pro besplatno."
    assert (
        "enterprise" not in prices
    )  # the price “on request” — we do not invent a number


def test_help_json_ld_faqpage_from_real_faq_section():
    resp = _client().get("/help")
    blocks = _json_ld_blocks(resp.text)
    assert len(blocks) == 1
    faq = blocks[0]
    assert faq["@type"] == "FAQPage"
    assert len(faq["mainEntity"]) > 0
    assert faq["mainEntity"][0]["@type"] == "Question"


def test_legal_page_has_no_json_ld():
    """Legal pages get only canonical/hreflang; we do not invent JSON-LD there."""
    resp = _client().get("/legal/privacy")
    assert "application/ld+json" not in resp.text
