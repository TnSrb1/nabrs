"""The service marker of the documents (``### file name``, ``documents_fetch.py``) must not leak to the client as text — both points of showing the description (the cabinet detail/htmx fragment — ``_tender_description.html``, the public card — ``tender.html``) must draw it as the heading of the document.

The parser (``src/parsers/jnportal/documents_fetch.py``) is NOT touched — the marker already lies in the DB, the RENDER defect is fixed by the shared predicate ``src.domain.description_render.split_description_blocks``.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from fastapi.testclient import TestClient

from src.api import deps
from src.api.cabinet import routes_tender_ai
from src.api.cabinet.tender_ai_service import TenderAnalyses
from src.api.main import app
from src.domain.description_render import split_description_blocks
from tests.api.conftest import FakeResult, FakeSession

_TENDER = "22222222-2222-2222-2222-222222222222"
_PROFILE = "33333333-3333-3333-3333-333333333333"

_DOC1 = "Uputstvo ponudjacima kako da sacine ponudu.pdf"
_DOC2 = "Obrazac strukture ponudene cene.xlsx"
_TWO_DOC_DESCRIPTION = (
    f"### {_DOC1}\n"
    "Prvi deo teksta tehnicke specifikacije.\n"
    "\n"
    f"### {_DOC2}\n"
    "Drugi deo teksta o strukturi cene."
)

_TRAP_NAME = "Uputstvo <img src=x onerror=alert(1)>.pdf"
_TRAP_DESCRIPTION = f"### {_TRAP_NAME}\nTekst dokumenta."


def _async(return_value):
    async def _stub(*args, **kwargs):
        return return_value

    return _stub


# --------------------------------------------------------------------------- #
#  Domain: parsing of the blocks (without DB/HTTP)
# --------------------------------------------------------------------------- #


def test_split_description_blocks_two_documents():
    blocks = split_description_blocks(_TWO_DOC_DESCRIPTION)
    assert [b.heading for b in blocks] == [_DOC1, _DOC2]
    assert blocks[0].text == "Prvi deo teksta tehnicke specifikacije."
    assert blocks[1].text == "Drugi deo teksta o strukturi cene."
    assert not any("###" in (b.heading or "") or "###" in b.text for b in blocks)


def test_split_description_blocks_no_marker_is_single_plain_block():
    """33% of descriptions without a marker (not jnportal) — must not lose the text."""
    blocks = split_description_blocks("Obican tekst bez markera.")
    assert len(blocks) == 1
    assert blocks[0].heading is None
    assert blocks[0].text == "Obican tekst bez markera."


def test_split_description_blocks_none_and_empty():
    assert split_description_blocks(None) == []
    assert split_description_blocks("") == []


# --------------------------------------------------------------------------- #
#  The cabinet — the tender detail AND the htmx fragment (the same partial, _tender_description.html)
# --------------------------------------------------------------------------- #

SESS = {"uid": "u-1", "tid": "t-1"}


def _cabinet_data(description):
    return TenderAnalyses(
        tender_id=_TENDER,
        profile_id=_PROFILE,
        profile_name="ИТ набавке",
        title="Набавка рачунара",
        customer_name="Општина Нови Сад",
        deadline_at=datetime(2026, 3, 15, 12, 0, tzinfo=timezone.utc),
        source_url="https://portal.rs/t/1",
        outputs={},
        description=description,
    )


def _cabinet_client(*, sess=SESS):
    def factory():
        return FakeSession()

    app.dependency_overrides[deps.get_session_factory] = lambda: factory
    app.dependency_overrides[deps.get_optional_session] = lambda: sess
    app.dependency_overrides[deps.get_redis] = lambda: object()
    return TestClient(app, follow_redirects=False)


def teardown_function():
    app.dependency_overrides.clear()


def test_cabinet_detail_shows_doc_headings_not_marker(monkeypatch):
    monkeypatch.setattr(
        routes_tender_ai.svc,
        "get_tender_analyses",
        _async(_cabinet_data(_TWO_DOC_DESCRIPTION)),
    )
    resp = _cabinet_client().get(f"/app/tenders/{_TENDER}?profile_id={_PROFILE}")
    assert resp.status_code == 200
    assert "###" not in resp.text
    assert _DOC1 in resp.text
    assert _DOC2 in resp.text
    assert "Prvi deo teksta tehnicke specifikacije." in resp.text


def test_cabinet_fragment_matches_detail_rendering(monkeypatch):
    """Round-trip (the pitfall #484): the markup of the htmx fragment = the markup of the primary
    render — the same partial ``_tender_description.html``."""
    monkeypatch.setattr(
        routes_tender_ai.svc,
        "get_tender_analyses",
        _async(_cabinet_data(_TWO_DOC_DESCRIPTION)),
    )
    detail = _cabinet_client().get(f"/app/tenders/{_TENDER}?profile_id={_PROFILE}")
    fragment = _cabinet_client().get(
        f"/app/tenders/{_TENDER}/description?profile_id={_PROFILE}&lang=sr"
    )
    assert detail.status_code == 200
    assert fragment.status_code == 200
    assert "###" not in fragment.text

    def _body(html: str) -> str:
        m = re.search(
            r'<div id="tender-description-body"[^>]*>(.*?)</div>',
            html,
            re.S,
        )
        assert m, "the body of the description not found"
        return m.group(1)

    assert _body(detail.text) == _body(fragment.text)


def test_cabinet_docs_only_is_collapsed_block(monkeypatch):
    """The cabinet shows the description of excerpts-only the same way as the public card:
    a collapsed <details class="tp-docs"> (summary — the first child), the file names
    inside it, not a bare caption at the top."""
    monkeypatch.setattr(
        routes_tender_ai.svc,
        "get_tender_analyses",
        _async(_cabinet_data(_TWO_DOC_DESCRIPTION)),
    )
    resp = _cabinet_client().get(f"/app/tenders/{_TENDER}?profile_id={_PROFILE}")
    assert resp.status_code == 200
    details = resp.text.index('<details class="tp-docs">')
    summary = resp.text.index("<summary>Из документации</summary>", details)
    assert details < summary < resp.text.index(_DOC1)
    assert resp.text.index("</details>", details) > resp.text.index(_DOC2)


def test_cabinet_description_escapes_trap_filename(monkeypatch):
    monkeypatch.setattr(
        routes_tender_ai.svc,
        "get_tender_analyses",
        _async(_cabinet_data(_TRAP_DESCRIPTION)),
    )
    resp = _cabinet_client().get(f"/app/tenders/{_TENDER}?profile_id={_PROFILE}")
    assert resp.status_code == 200
    assert "<img src=x onerror=alert(1)>" not in resp.text
    assert "&lt;img src=x onerror=alert(1)&gt;" in resp.text


# --------------------------------------------------------------------------- #
#  The public (SEO-indexable) card — tender.html
# --------------------------------------------------------------------------- #


def _tender(**kw):
    base = dict(
        id="11111111-1111-4111-8111-111111111111",
        title="Видеонаблюдение за школу",
        customer_name="ОШ «Вук Караджич»",
        amount_eur=12345.0,
        is_amount_estimated=False,
        deadline_at=datetime.now(timezone.utc) + timedelta(days=20),
        status="active",
        regions=["RS-00"],
        procedure_type=None,
        description=None,
        classifications=None,
        source_url="https://jnportal.rs/t/1",
        canonical_tender_id=None,
    )
    base.update(kw)
    return SimpleNamespace(**base)


def _public_client(session: FakeSession, *, sess=None):
    def factory():
        return session

    app.dependency_overrides[deps.get_session_factory] = lambda: factory
    app.dependency_overrides[deps.get_optional_session] = lambda: sess
    return TestClient(app)


def test_public_tender_page_shows_doc_headings_not_marker():
    tender = _tender(description=_TWO_DOC_DESCRIPTION)
    session = FakeSession(results=[FakeResult(items=[tender])])
    client = _public_client(session)
    resp = client.get("/tender/11111111-1111-4111-8111-111111111111")
    assert resp.status_code == 200
    assert "###" not in resp.text
    assert _DOC1 in resp.text
    assert _DOC2 in resp.text
    assert "tp-desc__doc-title" in resp.text


def test_public_tender_page_long_description_shows_doc_headings_not_marker():
    """The collapsed (clamp) view — the same ``.tp-desc__full``, the marker must not leak either, and the heading must stay inline (not <p>/<div>), otherwise ``-webkit-line-clamp`` counts the lines incorrectly."""
    long_tail = "Тек. " * 200
    description = _TWO_DOC_DESCRIPTION + "\n" + long_tail
    tender = _tender(description=description)
    session = FakeSession(results=[FakeResult(items=[tender])])
    client = _public_client(session)
    resp = client.get("/tender/11111111-1111-4111-8111-111111111111")
    assert resp.status_code == 200
    assert "tp-desc--clamp" in resp.text
    assert "###" not in resp.text
    assert _DOC1 in resp.text
    assert _DOC2 in resp.text
    full = re.search(
        r'<div class="tp-desc tp-desc__full">(.*?)</div>\s*<label', resp.text, re.S
    )
    assert full, "tp-desc__full not found"
    assert "<p" not in full.group(1) and "<div" not in full.group(1)
    assert "<strong" in full.group(1)


def test_public_tender_page_escapes_trap_filename():
    tender = _tender(description=_TRAP_DESCRIPTION)
    session = FakeSession(results=[FakeResult(items=[tender])])
    client = _public_client(session)
    resp = client.get("/tender/11111111-1111-4111-8111-111111111111")
    assert resp.status_code == 200
    assert "<img src=x onerror=alert(1)>" not in resp.text
    assert "&lt;img src=x onerror=alert(1)&gt;" in resp.text


def test_public_tender_page_without_marker_is_unaffected():
    """33% of descriptions without a marker (another portal) — the render does not change."""
    tender = _tender(description="Obican opis bez markera dokumenta.")
    session = FakeSession(results=[FakeResult(items=[tender])])
    client = _public_client(session)
    resp = client.get("/tender/11111111-1111-4111-8111-111111111111")
    assert resp.status_code == 200
    assert "Obican opis bez markera dokumenta." in resp.text
    assert "tp-desc__doc-title" not in resp.text


# --------------------------------------------------------------------------- #
#  F-05 of the UX audit 10.10: the service `# Sheet` lines of the extractor are not visible to the client
# --------------------------------------------------------------------------- #

# The look of a real production description (xlsx without PII): the file name + `# Sheet1` + a table.
_XLSX_DESCRIPTION = (
    "### SANITET - specifikacija.xlsx\n"
    "# Sheet1\n"
    "1 | 2 | 3\n"
    "ACIDI BORICI 3% 100ml | 30\n"
    "# Sheet2\n"
    "Ukupno | 33\n"
    "\n"
    "### Opis predmeta.docx\n"
    "Opis i specifikacija predmeta."
)


def test_extract_sheet_markers_hidden_from_blocks():
    blocks = split_description_blocks(_XLSX_DESCRIPTION)
    joined = "\n".join(b.text for b in blocks)
    assert "# Sheet" not in joined
    assert blocks[0].heading == "SANITET - specifikacija.xlsx"
    assert blocks[0].text == "1 | 2 | 3\nACIDI BORICI 3% 100ml | 30\nUkupno | 33"


def test_legit_hash_line_stays_visible():
    """A legitimate document text “# 1 Subject” is NOT a service marker of the extractor
    (it writes only ``# Sheet…`` / ``# List…`` / ``# Table N``)."""
    blocks = split_description_blocks("### Odluka.docx\n# 1 Predmet nabavke\nTekst.")
    assert blocks[0].text == "# 1 Predmet nabavke\nTekst."


def test_docs_only_detected():
    from src.domain.description_render import is_docs_only

    assert is_docs_only(split_description_blocks(_XLSX_DESCRIPTION)) is True
    assert is_docs_only(split_description_blocks("Nabavka goriva.")) is False
    assert is_docs_only([]) is False


def test_public_html_filter_has_no_sheet_marker():
    from src.api.templating import _description_html

    html = str(_description_html(_XLSX_DESCRIPTION))
    assert "# Sheet" not in html
    assert "ACIDI BORICI 3% 100ml" in html


def test_public_card_docs_only_is_collapsed_block():
    """A description of excerpts-only files — a collapsed <details> “From the documentation”
    in the page itself (not only a filter), the file names — inside it."""
    tender = _tender(description=_TWO_DOC_DESCRIPTION)
    session = FakeSession(results=[FakeResult(items=[tender])])
    resp = _public_client(session).get("/tender/11111111-1111-4111-8111-111111111111")
    assert resp.status_code == 200
    details = resp.text.index('<details class="tp-docs">')
    summary = resp.text.index("<summary>Iz dokumentacije</summary>", details)
    assert details < summary < resp.text.index(_DOC1)
    assert resp.text.index("</details>", details) > resp.text.index(_DOC2)


def test_public_card_plain_description_is_not_collapsed():
    tender = _tender(description="Nabavka goriva za vozila.")
    session = FakeSession(results=[FakeResult(items=[tender])])
    resp = _public_client(session).get("/tender/11111111-1111-4111-8111-111111111111")
    assert resp.status_code == 200
    assert '<details class="tp-docs">' not in resp.text
    assert "Nabavka goriva za vozila." in resp.text


# --------------------------------------------------------------------------- #
#  A description of service markers only — nothing is rendered (neither an empty div,
#  nor the raw `# Sheet1` lines)
# --------------------------------------------------------------------------- #

_MARKERS_ONLY_DESCRIPTION = "# Sheet1\n# List1"


def test_description_html_markers_only_renders_nothing():
    from src.api.templating import _description_html

    assert str(_description_html(_MARKERS_ONLY_DESCRIPTION)) == ""


def test_public_tender_page_markers_only_renders_no_block():
    tender = _tender(description=_MARKERS_ONLY_DESCRIPTION)
    session = FakeSession(results=[FakeResult(items=[tender])])
    client = _public_client(session)
    resp = client.get("/tender/11111111-1111-4111-8111-111111111111")
    assert resp.status_code == 200
    assert "# Sheet1" not in resp.text
    assert 'class="tp-desc"' not in resp.text
    assert "tp-desc--clamp" not in resp.text


def test_cabinet_detail_markers_only_renders_no_body(monkeypatch):
    monkeypatch.setattr(
        routes_tender_ai.svc,
        "get_tender_analyses",
        _async(_cabinet_data(_MARKERS_ONLY_DESCRIPTION)),
    )
    resp = _cabinet_client().get(f"/app/tenders/{_TENDER}?profile_id={_PROFILE}")
    assert resp.status_code == 200
    assert "# Sheet1" not in resp.text
    assert 'id="tender-description-body"' not in resp.text
