"""The template creation wizard (owner 22.09.2026): three steps of one form + a live counter.

The DB and session are mocked (ADR-013), templates render for real. Here we check exactly
what is promised to the client: not a single field got lost in the split into
steps, without JS the form stays a questionnaire, and the step’s figure is the same as on the showcase card (one ``preview_service.preview_count``, not a second counter).
"""

from __future__ import annotations

import re
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from src.api import deps
from src.api.cabinet import preview_service, template_prefill
from src.api.main import app
from tests.api.conftest import FakeResult, FakeSession

SESS = {"uid": "u-1", "tid": "t-1"}

# The form contract: exactly these 21 fields go into parse_profile_form (18 + the subject type
# and two customer lists, 0046). The list —
# a LITERAL from the measurement of the markup, not a derivation from the template: an assertion computed
# from the same source as the code is green for any content.
FORM_FIELDS = [
    "name",
    "keywords_include",
    "keywords_exclude",
    "keyword_dict",
    "match_all",
    "cls_include",
    "cls_exclude",
    "regions",
    "min_amount_eur",
    "max_amount_eur",
    "include_no_amount",
    "contract_types",
    "customers_include",
    "customers_exclude",
    "min_days_until_deadline",
    "enabled_source_keys",
    "delivery_channels",
    "delivery_schedule",
    "ai_analysis_level",
    "is_active",
    "debug_match",
]


def teardown_function():
    app.dependency_overrides.clear()


def _client(*, sess=None, results=None, user_locale=None):
    def factory():
        return FakeSession(results=list(results or []), user_locale=user_locale)

    app.dependency_overrides[deps.get_session_factory] = lambda: factory
    app.dependency_overrides[deps.get_optional_session] = lambda: sess
    return TestClient(app, follow_redirects=False)


def _field_names(html: str) -> set[str]:
    """The fields of the TEMPLATE FORM: elements with the attribute form= belong to other forms
    (the channel linking on step 3) and do not go into POST /profiles."""
    tags = re.findall(r"<(?:input|select|textarea)\b[^>]*>", html)
    return {
        m.group(1)
        for tag in tags
        if " form=" not in tag and (m := re.search(r'name="([a-z_]+)"', tag))
    }


# --------------------------------------------------------------------------- #
# Step 1: the split into three steps does not lose fields
# --------------------------------------------------------------------------- #
def test_wizard_keeps_all_eighteen_fields():
    """Not a single one of the 18 fields was lost or renamed in the split."""
    resp = _client(sess=SESS).get("/profiles/new?manual=1")
    assert resp.status_code == 200
    assert _field_names(resp.text) == set(FORM_FIELDS)


def test_wizard_renders_three_steps_and_advanced_block():
    resp = _client(sess=SESS).get("/profiles/new?manual=1")
    html = resp.text
    for step in ("1", "2", "3"):
        assert f'data-step="{step}"' in html, f"there is no section of step {step}"
    assert 'id="advancedBlock"' in html
    # The section headings are real headings, not paragraphs: before the fix there was not
    # a single <h2>/<h3> in the form.
    assert len(re.findall(r"<h2|<h3", html)) >= 4


def test_advanced_summary_is_first_child_of_details():
    """‼️ <summary> not as the first child — the UA hides the content of <details> entirely."""
    html = _client(sess=SESS).get("/profiles/new?manual=1").text
    block = html[html.index('id="advancedBlock"') :]
    block = block[: block.index("</details>")]
    assert re.search(r"^[^<]*<summary", block[block.index(">") + 1 :], re.S)


def test_form_is_single_post_to_profiles():
    """The steps are sections of ONE form: there are no server steps and no intermediate state."""
    html = _client(sess=SESS).get("/profiles/new?manual=1").text
    forms = re.findall(r"<form\b[^>]*>", html)
    profile_forms = [f for f in forms if 'action="/profiles"' in f]
    assert len(profile_forms) == 1
    assert '<form method="post" action="/profiles"' in html
    # The other forms — only the channel linking (step 3), empty, outside the template form.
    others = sorted(
        re.search(r'action="([^"]+)"', f).group(1)
        for f in forms
        if f not in profile_forms
    )
    assert others in ([], ["/cabinet/email/link", "/cabinet/telegram/link#kanali"])


def test_without_js_every_step_is_visible_and_controls_hidden():
    """Progressive enhancement: without JS all sections are expanded, the step buttons are hidden.

    The markup gives the steps WITHOUT the hidden class (the script hides them), and the step bar and the “Next/Back” buttons — with hidden: without the script there is nothing to page through, but the form can still be saved.
    """
    html = _client(sess=SESS).get("/profiles/new?manual=1").text
    for section in re.findall(r"<(?:section|details)[^>]*data-step=\"\d\"[^>]*>", html):
        assert "hidden" not in section, section
    assert re.search(r'<ol id="wizardNav"[^>]*class="[^"]*hidden', html)
    for row in re.findall(r"<div[^>]*data-wizard-actions[^>]*>", html):
        assert "hidden" in row, row
    # The save button in the markup is visible — only the script hides it.
    assert re.search(r'<div class="form-actions" data-form-submit>', html)


def test_edit_form_is_not_a_wizard(monkeypatch):
    """Editing an existing template — the whole form at once, without paging."""
    from src.api.cabinet import routes_profiles

    async def _get_profile(*a, **kw):
        return SimpleNamespace(
            id="p-1",
            name="Стройка",
            is_active=True,
            ai_analysis_level="none",
            regions=[],
            delivery_channels=["email"],
            delivery_schedule="immediate",
            enabled_source_keys=["jnportal"],
            classifications={},
            min_amount_eur=None,
            max_amount_eur=None,
            min_days_until_deadline=None,
            include_no_amount=False,
            keywords_include=None,
            keywords_exclude=None,
            keyword_dict=False,
            debug_match=False,
            match_all=False,
        )

    async def _downgrade(*a, **kw):
        return []

    monkeypatch.setattr(routes_profiles.svc, "get_profile", _get_profile)
    monkeypatch.setattr(routes_profiles.svc, "downgrade_notice", _downgrade)
    html = _client(sess=SESS).get("/profiles/p-1/edit").text
    assert 'data-wizard="1"' not in html
    assert _field_names(html) == set(FORM_FIELDS)


# --------------------------------------------------------------------------- #
# Step 2: the live counter on steps 1 and 2
# --------------------------------------------------------------------------- #
def test_form_drives_counter_by_existing_endpoint():
    """The counter is the OUTPUT of an existing route in the UI, not a new endpoint."""
    html = _client(sess=SESS).get("/profiles/new?manual=1").text
    assert 'hx-get="/profiles/preview-count"' in html
    assert 'hx-include="#profileForm"' in html
    # The delay is mandatory: a request on every character is a full scan of the 30-day window.
    assert "delay:600ms" in html
    # A flight indicator — otherwise “it looks fine but does not work”.
    assert 'hx-indicator=".preview-live"' in html
    # Both output places: step 1 (hx-target) and step 2 (out-of-band).
    assert 'id="previewStep1"' in html and 'id="previewStep2"' in html
    assert 'hx-target="#previewStep1"' in html


def test_step3_has_no_counter():
    """The channels do not affect the selection — there must be no figure on step 3."""
    html = _client(sess=SESS).get("/profiles/new?manual=1").text
    step3 = html[html.index('data-step="3"') :]
    step3 = step3[: step3.index("</section>")]
    assert "preview-live" not in step3


_ROWS = [
    SimpleNamespace(
        id="t-0",
        title="Nabavka računarske opreme",
        customer_name=None,
        deadline_at=None,
        title_normalized="nabavka racunarske opreme",
        description_normalized="monitori i stampaci",
        has_region=True,
    )
]


def test_preview_count_htmx_returns_fragment_with_oob_second_step():
    """An HX request gives a FRAGMENT: the figure of step 1 + the out-of-band figure of step 2."""
    client = _client(sess=SESS, results=[FakeResult(items=_ROWS)])
    resp = client.get(
        "/profiles/preview-count?keywords_include=racunarske&enabled_source_keys=jnportal",
        headers={"HX-Request": "true"},
    )
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/html")
    assert "≈ 1 тендер за 30 дней" in resp.text
    assert 'id="previewStep2"' in resp.text
    assert 'hx-swap-oob="innerHTML"' in resp.text


def test_preview_count_json_contract_survives():
    """Without the HX header the route still gives JSON — the contract is not broken."""
    client = _client(sess=SESS, results=[FakeResult(items=_ROWS)])
    resp = client.get(
        "/profiles/preview-count?keywords_include=racunarske&enabled_source_keys=jnportal"
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["count"] == 1 and body["window_days"] == 30


def test_preview_fragment_zero_is_named_not_empty():
    """Zero is a separate text, not an empty plate."""
    client = _client(sess=SESS, results=[FakeResult(items=[])])
    resp = client.get(
        "/profiles/preview-count?keywords_include=racunarske&enabled_source_keys=jnportal",
        headers={"HX-Request": "true"},
    )
    assert "Пока ничего не подошло" in resp.text


def test_preview_fragment_speaks_interface_locale():
    """The fragment is localised through the cabinet catalog, not nailed in Russian in the JS."""
    client = _client(sess=SESS, results=[FakeResult(items=_ROWS)], user_locale="sr")
    resp = client.get(
        "/profiles/preview-count?keywords_include=racunarske&enabled_source_keys=jnportal",
        headers={"HX-Request": "true"},
    )
    assert "≈ 1 tender za 30 dana" in resp.text


@pytest.mark.asyncio
async def test_step_counter_equals_library_card_count():
    """The step's figure = the figure of the showcase card for the same package.

    Both sides are counted by one ``preview_service.preview_count`` on one payload from ``template_prefill`` — it is pinned here by a fact, not a comment: the card (the batch) and the form preview (the single count) give one number on one corpus.
    """
    pkg_payload = template_prefill.from_package(
        __import__("src.api.cabinet.vertical_packages", fromlist=["x"]).get_package(
            "gradjevina"
        ),
        "ru",
    )
    rows = [
        SimpleNamespace(
            title_normalized="radovi na rekonstrukciji skole",
            description_normalized="gradjevinski radovi, krov i fasada",
            idx=0,
        )
    ]
    card = await preview_service.preview_counts(
        FakeSession(results=[FakeResult(items=rows)]), [pkg_payload]
    )
    form = await preview_service.preview_count(
        FakeSession(results=[FakeResult(items=rows)]), pkg_payload
    )
    assert card[0] == form.count


# --------------------------------------------------------------------------- #
# Step 3: two clicks from the package to the template (the confirmation screen)
# --------------------------------------------------------------------------- #
def _async(value):
    async def _fn(*a, **kw):
        return value

    return _fn


def _no_live_channels(monkeypatch):
    from src.api.cabinet import routes_profiles

    monkeypatch.setattr(
        routes_profiles.channel_svc, "email_channel_state", _async(None)
    )
    monkeypatch.setattr(
        routes_profiles.channel_svc, "telegram_channel_state", _async(None)
    )


def test_confirm_screen_speaks_human_words(monkeypatch):
    """The confirmation screen retells the prefill in words, not by the form fields."""
    _no_live_channels(monkeypatch)
    client = _client(sess=SESS, results=[FakeResult(items=_ROWS)])
    resp = client.get("/profiles/new/confirm?from_package=gradjevina")
    assert resp.status_code == 200
    html = resp.text
    assert "вся Сербия" in html  # regions not selected
    assert "любая" in html  # the amount is not limited
    assert "Создать шаблон" in html


def test_confirm_screen_posts_all_eighteen_fields(monkeypatch):
    """The “Create” button sends the same POST /profiles with ALL the prefill fields."""
    _no_live_channels(monkeypatch)
    client = _client(sess=SESS, results=[FakeResult(items=_ROWS)])
    html = client.get("/profiles/new/confirm?from_package=gradjevina").text
    form = html[html.index('<form method="post" action="/profiles"') :]
    form = form[: form.index("</form>")]
    assert _field_names(form) == set(FORM_FIELDS)


def test_confirm_screen_keeps_way_to_full_form(monkeypatch):
    """The full questionnaire — by the link “Change settings”, with the same source."""
    _no_live_channels(monkeypatch)
    client = _client(sess=SESS, results=[FakeResult(items=_ROWS)])
    html = client.get("/profiles/new/confirm?from_package=gradjevina").text
    assert 'href="/profiles/new?from_package=gradjevina"' in html


def test_confirm_screen_shows_same_count_as_card(monkeypatch):
    """The figure of the confirmation screen — the same preview_count on the same payload."""
    _no_live_channels(monkeypatch)
    client = _client(sess=SESS, results=[FakeResult(items=_ROWS)])
    html = client.get("/profiles/new/confirm?preset=all").text
    assert "≈ 1 тендер за 30 дней" in html


# --------------------------------------------------------------------------- #
# Step 5: the result screen
# --------------------------------------------------------------------------- #
def _saved_profile(**kw):
    base = dict(
        id="p-1",
        name="Стройка",
        is_active=True,
        enabled_source_keys=["jnportal"],
        regions=[],
        classifications={},
        min_amount_eur=None,
        max_amount_eur=None,
        min_days_until_deadline=None,
        include_no_amount=False,
        keywords_include=[{"all_of": ["racunarske"]}],
        keywords_exclude=[],
        keyword_dict=False,
        match_all=False,
        delivery_channels=["email"],
    )
    base.update(kw)
    return SimpleNamespace(**base)


_EXAMPLE_ROWS = [
    SimpleNamespace(
        id="t-1",
        title="Nabavka racunarske opreme",
        title_normalized="nabavka racunarske opreme",
        description_normalized="monitori",
        customer_name="Škola „Vuk Karadžić“",
        deadline_at=None,
        archived=True,
    )
]


def test_created_screen_shows_count_and_examples(monkeypatch):
    """“Done — N matched” + the first three examples with links to the cards."""
    from src.api.cabinet import routes_profiles

    monkeypatch.setattr(routes_profiles.svc, "get_profile", _async(_saved_profile()))
    client = _client(
        sess=SESS,
        results=[FakeResult(items=_EXAMPLE_ROWS)],
    )
    resp = client.get("/profiles/p-1/created")
    assert resp.status_code == 200
    assert "≈ 1 тендер за 30 дней" in resp.text
    # The profile word “racunarske” is highlighted in the example heading.
    assert "Nabavka <mark>racunarske</mark> opreme" in resp.text
    # ‼️ The public card: /app/tenders/{id} without profile_id gives “not found”
    # (routes_tender_ai.tender_detail), the former assertion pinned a broken link.
    assert 'href="/tender/t-1"' in resp.text
    assert "/app/tenders/t-1" not in resp.text


def test_created_screen_zero_has_its_own_text(monkeypatch):
    """N = 0 — a named text with a way out, not an empty screen."""
    from src.api.cabinet import routes_profiles

    monkeypatch.setattr(routes_profiles.svc, "get_profile", _async(_saved_profile()))
    client = _client(sess=SESS, results=[FakeResult(items=[])])
    html = client.get("/profiles/p-1/created").text
    assert "Пока ничего не подошло" in html
    assert "убрать фильтр суммы" in html


def test_created_screen_open_now_lists_open_tenders(monkeypatch):
    """The block “Already open”: the link to the card and the “from the archive” mark are always visible."""
    from src.api.cabinet import routes_profiles

    monkeypatch.setattr(routes_profiles.svc, "get_profile", _async(_saved_profile()))
    # The first SELECT — the counter (empty), the second — “open now”.
    client = _client(
        sess=SESS, results=[FakeResult(items=[]), FakeResult(items=_EXAMPLE_ROWS)]
    )
    html = client.get("/profiles/p-1/created").text
    assert "Уже открытые тендеры по этому шаблону" in html
    # The mark is on the row of the archived tender (one archived in the fixture), not in a general paragraph.
    assert html.count("из архива — уведомления не будет") == 1
    assert 'href="/tender/t-1"' in html


def test_created_screen_open_now_hides_archive_note_when_not_archived(monkeypatch):
    """A tender after the delivery cut-off — a notification will come, the “from the archive” mark is not needed."""
    from src.api.cabinet import routes_profiles

    monkeypatch.setattr(routes_profiles.svc, "get_profile", _async(_saved_profile()))
    fresh = [SimpleNamespace(**{**vars(_EXAMPLE_ROWS[0]), "archived": False})]
    client = _client(sess=SESS, results=[FakeResult(items=[]), FakeResult(items=fresh)])
    html = client.get("/profiles/p-1/created").text
    assert 'href="/tender/t-1"' in html
    assert "из архива — уведомления не будет" not in html


def test_created_screen_open_now_empty_is_honest(monkeypatch):
    """Empty → it is said directly that new ones will come by notification, and a link to the edit."""
    from src.api.cabinet import routes_profiles

    monkeypatch.setattr(routes_profiles.svc, "get_profile", _async(_saved_profile()))
    client = _client(sess=SESS, results=[FakeResult(items=[]), FakeResult(items=[])])
    html = client.get("/profiles/p-1/created").text
    assert (
        "Сейчас открытых тендеров нет. Новые придут уведомлением, как только появятся."
        in html
    )
    assert 'href="/profiles/p-1/edit"' in html


def test_detail_shows_open_now_only_after_update(monkeypatch):
    """After the edit (?updated=1) the block is there; on an ordinary opening of the card — it is not."""
    from src.api.cabinet import routes_profiles

    monkeypatch.setattr(routes_profiles.svc, "get_profile", _async(_saved_profile()))
    monkeypatch.setattr(routes_profiles.svc, "downgrade_notice", _async([]))
    client = _client(sess=SESS, results=[FakeResult(items=_EXAMPLE_ROWS)])
    assert "Уже открытые тендеры" in client.get("/profiles/p-1?updated=1").text
    client = _client(sess=SESS, results=[])
    assert "Уже открытые тендеры" not in client.get("/profiles/p-1").text


def _warning_stub(monkeypatch, value):
    from src.api.cabinet import routes_profiles

    async def _w(*a, **k):
        return value

    monkeypatch.setattr(routes_profiles.channel_svc, "delivery_warning", _w)


def test_created_screen_shows_no_channel_warning(monkeypatch):
    """‼️ Branch #531: a template without a channel must give a VISIBLE warning."""
    from src.api.cabinet import routes_profiles

    monkeypatch.setattr(
        routes_profiles.svc, "get_profile", _async(_saved_profile(delivery_channels=[]))
    )
    _warning_stub(monkeypatch, {"no_channel": True, "channelless": []})
    client = _client(sess=SESS, results=[FakeResult(items=_EXAMPLE_ROWS)])
    html = client.get("/profiles/p-1/created").text
    assert (
        "Тендеры видны в кабинете: подключите Telegram или e-mail, чтобы получать уведомления"
        in html
    )
    assert 'href="/cabinet/account#kanali"' in html


def test_created_screen_without_warning_has_no_banner(monkeypatch):
    from src.api.cabinet import routes_profiles

    monkeypatch.setattr(routes_profiles.svc, "get_profile", _async(_saved_profile()))
    _warning_stub(monkeypatch, None)
    client = _client(sess=SESS, results=[FakeResult(items=_EXAMPLE_ROWS)])
    html = client.get("/profiles/p-1/created").text
    assert "data-no-channel" not in html


def test_detail_shows_channelless_warning_with_edit_link(monkeypatch):
    """Case (b) on the template page: a row with the name + a button to edit."""
    from src.api.cabinet import routes_profiles

    monkeypatch.setattr(routes_profiles.svc, "get_profile", _async(_saved_profile()))
    monkeypatch.setattr(routes_profiles.svc, "downgrade_notice", _async([]))
    monkeypatch.setattr(routes_profiles.svc, "list_profiles", _async([]))
    _warning_stub(
        monkeypatch,
        {"no_channel": False, "channelless": [{"id": "p-1", "name": "Икс"}]},
    )
    html = _client(sess=SESS, results=[]).get("/profiles/p-1").text
    assert "У шаблона «Икс» нет канала доставки" in html
    assert 'href="/profiles/p-1/edit"' in html


def test_examples_come_from_the_same_query_as_count():
    """The examples — the same pass as the counter, not a second query for the “similar”.

    There is no separate _EXAMPLES_SQL any more: the display columns travel in _PREVIEW_SQL, the ORDER BY is mandatory (without it “the first five” are non-deterministic).
    """
    from src.workers.matching import PREVIEW_WHERE_A

    assert not hasattr(preview_service, "_EXAMPLES_SQL")
    sql = str(preview_service._PREVIEW_SQL)
    assert PREVIEW_WHERE_A in sql
    assert "ORDER BY tn.first_seen_at DESC, tn.id" in sql
    for col in ("tn.title,", "tn.customer_name,", "tn.deadline_at"):
        assert col in sql


def test_confirm_form_roundtrips_into_the_same_payload(monkeypatch):
    """The created template = the one shown: the hidden form is parsed into the same payload.

    We take the HTML of the confirmation screen, assemble a form from its hidden fields and run it through the same ``parse_profile_form`` that POST /profiles calls. If the serialization diverged in even one field (the amount “None”, lost regions, a checkbox without a value), the client would get not the template they confirmed.
    """
    import html as html_mod

    from starlette.datastructures import FormData

    from src.api.forms import parse_profile_form

    _no_live_channels(monkeypatch)
    client = _client(sess=SESS, results=[FakeResult(items=_ROWS)])
    page = client.get("/profiles/new/confirm?from_package=gradjevina").text
    form_html = page[page.index('<form method="post" action="/profiles"') :]
    form_html = form_html[: form_html.index("</form>")]

    items: list[tuple[str, str]] = []
    for name, value in re.findall(
        r'<input type="hidden" name="([a-z_]+)" value="([^"]*)">', form_html
    ):
        items.append((name, html_mod.unescape(value)))
    for name, value in re.findall(
        r'<textarea name="([a-z_]+)" class="hidden">(.*?)</textarea>',
        form_html,
        re.S,
    ):
        items.append((name, html_mod.unescape(value)))
    # The sources — the ticked checkboxes (as the browser will send them).
    for name, value in re.findall(
        r'<input type="checkbox" name="([a-z_]+)" value="([^"]*)" checked>', form_html
    ):
        items.append((name, html_mod.unescape(value)))
    assert {n for n, _ in items} == set(FORM_FIELDS)

    payload, errors = parse_profile_form(FormData(items))
    assert errors == []

    pkg = __import__("src.api.cabinet.vertical_packages", fromlist=["x"]).get_package(
        "gradjevina"
    )
    # The sources of the confirmation screen — from the `sources` registry (SRC-0a); FakeSession
    # by default gives the production fact of 30.09: exactly jnportal is enabled.
    expected = template_prefill.from_package(pkg, "ru", source_keys=["jnportal"])
    expected["delivery_channels"] = []
    for key in (
        "name",
        "keywords_include",
        "keywords_exclude",
        "classifications",
        "regions",
        "enabled_source_keys",
        "delivery_channels",
        "match_all",
        "keyword_dict",
        "min_amount_eur",
        "max_amount_eur",
        "min_days_until_deadline",
        "include_no_amount",
        "is_active",
    ):
        assert payload[key] == expected[key], key
