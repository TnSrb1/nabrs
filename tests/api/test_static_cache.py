"""The static version in the address and Cache-Control (TASK static-cache).

Expectations — literals from the server’s response (the header, the code, the presence of ``?v=``), not a recount from the model of the code. The version in the address is taken from the markup itself, i.e. from what the browser sees.
"""

from __future__ import annotations

import pathlib
import re

import pytest
from fastapi.testclient import TestClient

from src.api import deps
from src.api.main import app
from src.api.static_assets import file_version
from tests.api.conftest import FakeSession

_STATIC_REF = re.compile(r"/static/[^\"'\s<>)]+")
_VERSIONED = re.compile(r"/static/[^?\"'\s<>)]+\?v=[0-9a-f]{10}$")


def teardown_function():
    app.dependency_overrides.clear()


def _client() -> TestClient:
    app.dependency_overrides[deps.get_session_factory] = lambda: (lambda: FakeSession())
    app.dependency_overrides[deps.get_optional_session] = lambda: None
    return TestClient(app, follow_redirects=False)


@pytest.mark.parametrize("path", ["/", "/help", "/plans", "/login"])
def test_every_static_reference_in_page_is_versioned(path):
    html = _client().get(path).text
    refs = _STATIC_REF.findall(html)
    assert refs, f"{path}: the page has not a single link to /static/"
    unversioned = [ref for ref in refs if not _VERSIONED.search(ref)]
    assert unversioned == [], f"{path}: without ?v= : {unversioned}"


def test_app_css_href_in_page_carries_version_and_serves_immutable():
    client = _client()
    html = client.get("/").text
    match = re.search(r'href="(/static/css/app\.css\?v=[0-9a-f]{10})"', html)
    assert match, "app.css in the markup without ?v="
    resp = client.get(match.group(1))
    assert resp.status_code == 200
    assert resp.headers["cache-control"] == "public, max-age=31536000, immutable"


def test_static_without_version_gets_short_cache():
    resp = _client().get("/static/css/app.css")
    assert resp.status_code == 200
    assert resp.headers["cache-control"] == "public, max-age=3600"


def test_static_with_stale_version_gets_short_cache():
    # The old address (before the file was edited) must not pin stale content.
    resp = _client().get("/static/css/app.css?v=0000000000")
    assert resp.status_code == 200
    assert resp.headers["cache-control"] == "public, max-age=3600"


def test_head_on_versioned_static_is_200_without_body():
    client = _client()
    html = client.get("/").text
    url = re.search(r'href="(/static/css/app\.css\?v=[0-9a-f]{10})"', html).group(1)
    resp = client.head(url)
    assert resp.status_code == 200
    assert resp.content == b""
    assert resp.headers["cache-control"] == "public, max-age=31536000, immutable"


def test_version_changes_with_file_content(tmp_path: pathlib.Path):
    asset = tmp_path / "a.css"
    asset.write_bytes(b"a")
    assert file_version(asset) == "ca978112ca"  # sha256(b"a")[:10]
    asset.write_bytes(b"b")
    assert file_version(asset) != "ca978112ca"
