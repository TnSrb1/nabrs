"""The translation toggle on the feed cards — the fragment ``GET /app/tenders/{id}/title``
(portal, Stage 5).

The DB/session/translation are mocked (ADR-013), the templates are rendered for real. The logic of
the translation itself is not duplicated (tests/ai/test_translation.py), nor is the detail card
contour (tests/api/test_tender_card_translation.py). Here — exactly what
Stage 5 introduces:

- ``lang=sr`` returns the original and does NOT call the model (otherwise the toggle back and forth would
  burn tokens on every click);
- ``lang=ru`` calls ``translate_tender`` with the text FROM THE SERVER and substitutes it;
- a translation refusal → the original + a quiet note, not a 500;
- the gate of ``get_tender_analyses`` (someone else’s/broken tender/profile) → 404;
- the heading remains a link to the tender in both states;
- the feed and teaser cards render the toggle, but the render does NOT call the translation.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from src.ai.translation import TenderTranslation, TranslationUsage
from src.api import deps
from src.api.cabinet import routes_tender_ai
from src.api.cabinet.tender_ai_service import TenderAnalyses
from src.api.main import app
from tests.api.conftest import FakeSession

SESS = {"uid": "u-1", "tid": "t-1"}
_TENDER = "22222222-2222-2222-2222-222222222222"
_PROFILE = "33333333-3333-3333-3333-333333333333"
_SOURCE_URL = "https://portal.rs/t/1"

_TITLE_SR = "Набавка рачунара"
_DESC_SR = "Предмет јавне набавке је испорука рачунарске опреме."
_TITLE_RU = "Закупка компьютеров"

_REDIS = object()  # marker: the route must pass the cache client into the translation
_TR = TenderTranslation(title=_TITLE_RU, description="Поставка оборудования.")

_FRAG = f"/app/tenders/{_TENDER}/title?profile_id={_PROFILE}"


def _data():
    return TenderAnalyses(
        tender_id=_TENDER,
        profile_id=_PROFILE,
        profile_name="ИТ набавке",
        title=_TITLE_SR,
        customer_name="Општина Нови Сад",
        deadline_at=datetime(2026, 3, 15, 12, 0, tzinfo=timezone.utc),
        source_url=_SOURCE_URL,
        outputs={},
        description=_DESC_SR,
    )


def _client(*, sess=SESS):
    def factory():
        return FakeSession()

    app.dependency_overrides[deps.get_session_factory] = lambda: factory
    app.dependency_overrides[deps.get_optional_session] = lambda: sess
    app.dependency_overrides[deps.get_redis] = lambda: _REDIS
    return TestClient(app, follow_redirects=False)


def teardown_function():
    app.dependency_overrides.clear()


def _async(return_value):
    async def _stub(*args, **kwargs):
        return return_value

    return _stub


class _TranslateSpy:
    """Substitutes translate_tender: counts the calls and remembers the arguments.

    Returns a PAIR ``(translation, usage)`` — the contract of translate_tender after the
    usage accounting was introduced (``TranslationUsage``); the gate/debit — in test_translation_gates.py.
    """

    def __init__(self, result, usage=TranslationUsage(calls=1, cost_usd=0.001)):
        self.result = result
        self.usage = usage
        self.calls: list[dict] = []

    async def __call__(self, **kwargs):
        self.calls.append(kwargs)
        return self.result, self.usage


_DEFAULT = object()  # “data not set” ≠ “data=None” (None = the gate refused)


def _setup(monkeypatch, *, translation_result=_TR, data=_DEFAULT):
    monkeypatch.setattr(
        routes_tender_ai.svc,
        "get_tender_analyses",
        _async(_data() if data is _DEFAULT else data),
    )
    spy = _TranslateSpy(translation_result)
    monkeypatch.setattr(routes_tender_ai.translation, "translate_tender", spy)
    return spy


# --------------------------------------------------------------------------- #
#  Original (lang=sr) — without the model
# --------------------------------------------------------------------------- #


def test_lang_sr_returns_original_without_calling_model(monkeypatch):
    """‼️ The reverse transition is free: going back to the original does not call the model."""
    spy = _setup(monkeypatch)
    resp = _client().get(f"{_FRAG}&lang=sr")

    assert resp.status_code == 200
    assert _TITLE_SR in resp.text
    assert _TITLE_RU not in resp.text
    assert spy.calls == [], "showing the original must not call the model"
    assert "Показать перевод" in resp.text


def test_default_lang_is_original_and_does_not_call_model(monkeypatch):
    """‼️ The owner’s rule: without ?lang= the fragment returns the ORIGINAL and does not call the model.

    The “Show translation” button sends an explicit lang=<interface language> — only then do we translate.
    """
    spy = _setup(monkeypatch)
    resp = _client().get(_FRAG)

    assert resp.status_code == 200
    assert _TITLE_SR in resp.text
    assert _TITLE_RU not in resp.text
    assert spy.calls == [], "the default — the original, the model is not called"
    assert "Показать перевод" in resp.text


# --------------------------------------------------------------------------- #
#  Translation (lang=ru)
# --------------------------------------------------------------------------- #


def test_lang_ru_calls_translate_and_shows_translation(monkeypatch):
    spy = _setup(monkeypatch)
    resp = _client().get(f"{_FRAG}&lang=ru")

    assert resp.status_code == 200
    assert _TITLE_RU in resp.text
    assert _TITLE_SR not in resp.text
    assert len(spy.calls) == 1
    assert "Оригинал (sr)" in resp.text
    assert "lang=sr" in resp.text


def test_translated_text_comes_from_server_not_from_request(monkeypatch):
    """‼️ The text that goes into the model comes FROM THE DB, not from the client’s query string.

    Otherwise anyone could push arbitrary text through our quota and store
    the result in the shared (not tenant-scoped) cache.
    """
    spy = _setup(monkeypatch)
    _client().get(f"{_FRAG}&lang=ru&title=ПОДСУНУТО&description=ПОДСУНУТО")

    call = spy.calls[0]
    assert call["title"] == _TITLE_SR
    assert call["tender_id"] == _TENDER
    assert call["redis"] is _REDIS
    assert "ПОДСУНУТО" not in str(call)


def test_fragment_translates_the_title_only_never_the_whole_tz(monkeypatch):
    """‼️ Stage 8 regression: the TITLE goes into the model, not the whole TOR.

    The root of the bug “the toggle does not translate”: the fragment draws one <a> with the title,
    but also sent the description to the model (``translation._MAX_INPUT_CHARS`` = 6000
    characters). On real TORs (46–50 KB of raw text in prod) generating 6000
    characters did not fit under the ``translate_tender`` ceiling — 20 s → TimeoutError
    → ``None`` → the card silently stayed in Serbian. From outside it looked like
    “the button is pressed and nothing happens”.

    It worked exactly for the cards whose ``description`` was empty — hence the report
    “only the top one is translated”.
    """
    spy = _setup(monkeypatch)
    resp = _client().get(f"{_FRAG}&lang=ru")

    assert resp.status_code == 200
    call = spy.calls[0]
    assert call["title"] == _TITLE_SR, "the title must go into the model"
    assert call["description"] is None, (
        "the description in the fragment’s response is not used — sending it to the model means "
        "hitting the timeout and silently returning the original"
    )
    assert _DESC_SR not in str(call)


def test_translation_failure_falls_back_to_original_not_500(monkeypatch):
    """A refusal of the model/cache is not an error: the original + a soft note."""
    _setup(monkeypatch, translation_result=None)
    resp = _client().get(f"{_FRAG}&lang=ru")

    assert resp.status_code == 200
    assert _TITLE_SR in resp.text
    assert "перевод недоступен" in resp.text
    # The button stays: a retry on a cache hit costs nothing.
    assert "Показать перевод" in resp.text


# --------------------------------------------------------------------------- #
#  The access gate is the same as on the detail page
# --------------------------------------------------------------------------- #


def test_foreign_or_missing_tender_404_and_never_translates(monkeypatch):
    """‼️ The gate BEFORE the translation: someone else’s tender is neither translated nor served from the cache."""
    spy = _setup(monkeypatch, data=None)
    resp = _client().get(f"{_FRAG}&lang=ru")

    assert resp.status_code == 404
    assert spy.calls == []


def test_missing_profile_id_404(monkeypatch):
    _setup(monkeypatch)
    resp = _client().get(f"/app/tenders/{_TENDER}/title?lang=ru")

    assert resp.status_code == 404


def test_unknown_view_404(monkeypatch):
    """view — from the allowlist: it selects the markup of the title, not the text."""
    _setup(monkeypatch)
    resp = _client().get(f"{_FRAG}&view=evil")

    assert resp.status_code == 404


def test_anonymous_gets_401_hx_redirect_not_a_fragment(monkeypatch):
    _setup(monkeypatch)
    resp = _client(sess=None).get(f"{_FRAG}&lang=ru")

    assert resp.status_code == 401
    assert resp.headers["HX-Redirect"] == "/login"


# --------------------------------------------------------------------------- #
#  The heading remains a link (the translation does not break the transition to the tender)
# --------------------------------------------------------------------------- #


def test_feed_view_title_stays_link_to_tender_in_both_states(monkeypatch):
    _setup(monkeypatch)
    client = _client()

    original = client.get(f"{_FRAG}&view=feed&lang=sr").text
    translated = client.get(f"{_FRAG}&view=feed&lang=ru").text

    for html in (original, translated):
        assert f'href="{_SOURCE_URL}"' in html
        assert 'target="_blank"' in html
        assert 'rel="noopener"' in html


def test_teaser_view_title_stays_link_to_cabinet_card(monkeypatch):
    """The teaser leads to the cabinet card (entry to the AI analysis), not to the portal."""
    _setup(monkeypatch)
    resp = _client().get(f"{_FRAG}&view=teaser&lang=ru")

    assert f'href="/app/tenders/{_TENDER}?profile_id={_PROFILE}"' in resp.text
    assert _SOURCE_URL not in resp.text


def test_fragment_swaps_itself_by_id(monkeypatch):
    """hx-target = its own container, swap=outerHTML — otherwise after a click
    the toggle would be duplicated/lost. The default kind is “feed” (the route), the id MUST
    include the view (defect 2 — otherwise the rows of “My tenders” on the board and in the folder
    would get the same id)."""
    _setup(monkeypatch)
    resp = _client().get(f"{_FRAG}&lang=sr")

    box = f"card-title-feed-{_TENDER}-{_PROFILE}"
    assert f'id="{box}"' in resp.text
    assert f'hx-target="#{box}"' in resp.text
    assert 'hx-swap="outerHTML"' in resp.text


# --------------------------------------------------------------------------- #
#  PR #484 follow-up — defect 1: the title style after the swap on “My tenders”
# --------------------------------------------------------------------------- #


def test_my_board_view_fragment_keeps_the_row_title_class(monkeypatch):
    """‼️ Defect 1: after clicking “Show translation” on a row of “My tenders”
    the title MUST remain ``fn-row__title`` — the same class as in the primary render of the row
    (``_my_tender_row.html``). Before the fix the fragment substituted ``agenda__title-link`` (a different meaning:
    ``color: inherit``, no bold) — the row “drifted” after the swap."""
    _setup(monkeypatch)
    resp = _client().get(f"{_FRAG}&view=my_board&lang=ru")

    assert resp.status_code == 200
    assert 'class="fn-row__title"' in resp.text
    assert "agenda__title-link" not in resp.text
    assert f'href="/app/tenders/{_TENDER}?profile_id={_PROFILE}"' in resp.text


def test_my_folder_view_fragment_keeps_the_row_title_class(monkeypatch):
    """The same invariant for a row inside a folder (view="my_folder")."""
    _setup(monkeypatch)
    resp = _client().get(f"{_FRAG}&view=my_folder&lang=ru")

    assert resp.status_code == 200
    assert 'class="fn-row__title"' in resp.text
    assert "agenda__title-link" not in resp.text


# --------------------------------------------------------------------------- #
#  PR #484 follow-up — defect 2: the id is unique and is reproduced after the swap
# --------------------------------------------------------------------------- #


def test_my_board_and_my_folder_views_round_trip_their_own_distinct_id(
    monkeypatch,
):
    """‼️ Defect 2: the same tender is drawn AT ONCE in a funnel stage
    (view=my_board) and in a folder (view=my_folder) on /app/my — both variants
    must get a DIFFERENT id, and this id MUST be REPRODUCED by the fragment
    after the swap (otherwise the second click misses the target, round trip)."""
    _setup(monkeypatch)
    client = _client()

    board_resp = client.get(f"{_FRAG}&view=my_board&lang=sr")
    folder_resp = client.get(f"{_FRAG}&view=my_folder&lang=sr")

    board_box = f"card-title-my_board-{_TENDER}-{_PROFILE}"
    folder_box = f"card-title-my_folder-{_TENDER}-{_PROFILE}"
    assert board_box != folder_box

    assert f'id="{board_box}"' in board_resp.text
    assert f'hx-target="#{board_box}"' in board_resp.text
    assert f'id="{folder_box}"' in folder_resp.text
    assert f'hx-target="#{folder_box}"' in folder_resp.text


# --------------------------------------------------------------------------- #
#  Cards: the toggle is there, the model is not called on render
# --------------------------------------------------------------------------- #


class _FeedItemStub:
    tender_id = _TENDER
    profile_id = _PROFILE
    title = _TITLE_SR
    tender_url = _SOURCE_URL
    customer_name = "Општина Нови Сад"
    profile_name = "ИТ набавке"
    deadline_at = None
    sent_at = None
    ai_actions = type(
        "A",
        (),
        {
            "delivered_analysis_level": None,
            "has_bid_verdict": False,
            "has_checklist": False,
            "has_draft": False,
        },
    )()


def _render_card(template: str) -> str:
    from src.api.templating import templates

    return templates.get_template(template).render(
        item=_FeedItemStub(),
        feed_items=[_FeedItemStub()],
        profiles=[object()],
        telegram_linked=True,
    )


def test_feed_card_renders_toggle_and_keeps_tender_link():
    html = _render_card("_feed_card.html")

    assert "Показать перевод" in html
    assert f"/app/tenders/{_TENDER}/title?profile_id={_PROFILE}" in html
    assert "view=feed" in html
    assert f'href="{_SOURCE_URL}"' in html
    assert 'target="_blank"' in html
    assert _TITLE_SR in html  # without a click — the original, the model was not called


def test_teaser_card_renders_toggle_and_keeps_cabinet_link():
    html = _render_card("_cockpit_feed_teaser.html")

    assert "Показать перевод" in html
    assert "view=teaser" in html
    assert f'href="/app/tenders/{_TENDER}?profile_id={_PROFILE}"' in html
    assert _TITLE_SR in html


# --------------------------------------------------------------------------- #
#  A full page: EACH card addresses its own title (Stage 8)
# --------------------------------------------------------------------------- #


def _multi_items(n: int) -> list:
    """n items of the feed of one profile — as in prod (the tenant has one profile)."""
    items = []
    for i in range(1, n + 1):
        it = _FeedItemStub()
        it.tender_id = f"{i}{i}{i}{i}{i}{i}{i}{i}-1111-1111-1111-111111111111"
        it.profile_id = _PROFILE  # ‼️ common: the uniqueness must be given by tender_id
        it.title = f"Набавка {i}"
        it.tender_url = f"https://portal.rs/t/{i}"
        items.append(it)
    return items


def _render_feed_page(items: list) -> str:
    """The full ``feed_archive.html`` (both urgency sections) — not a partial in isolation.

    The duplicate id is visible ONLY on the full page: the partials one by one are always “clean”.
    """
    import types

    from src.api.public.portal_filters import PortalFilters
    from src.api.templating import templates

    from starlette.datastructures import QueryParams

    # query_params — as in the real Request: the language switcher in the header
    # reads it (lang_switch_href) on each render of the page.
    request = types.SimpleNamespace(
        url=types.SimpleNamespace(path="/app/feed"),
        scope={},
        headers={},
        query_params=QueryParams(),
    )
    urgent, rest = items[: len(items) // 2], items[len(items) // 2 :]
    for it in urgent:  # “burning” — the deadline is within two days
        it.deadline_at = datetime.now(timezone.utc) + timedelta(days=1)
    return templates.get_template("feed_archive.html").render(
        request=request,
        items=items,
        urgent_items=urgent,
        rest_items=rest,
        page=1,
        total_pages=1,
        total=len(items),
        region_hidden=None,
        profiles=[],
        active_profile=None,
        pf=PortalFilters(default_sort="date", sort="date"),
        sort_keys=["date", "deadline", "amount_desc", "amount_asc"],
        region_options=[],
        source_options=[],
        timed_out=False,
        session={"uid": "u-1", "tid": "t-1"},
    )


def test_every_card_on_the_page_has_a_unique_id():
    """‼️ A duplicate DOM id would break the toggle on all cards except the first.

    ``querySelector('#id')`` returns the FIRST element: if the ids of two
    cards coincided, a click on the lower one would swap the upper one. We check ALL the ids of the page, not
    only ``card-title-*``: one can collide with anything on the page.
    """
    import re
    from collections import Counter

    html = _render_feed_page(_multi_items(4))

    dups = [i for i, n in Counter(re.findall(r'\sid="([^"]+)"', html)).items() if n > 1]
    assert dups == [], f"duplicate ids on the feed page: {dups}"


def test_every_cards_button_targets_its_own_title_not_the_first():
    """The hx-target of EACH button points to ITS OWN container — not only that of the first one."""
    import re

    items = _multi_items(4)
    html = _render_feed_page(items)

    boxes = re.findall(r'id="(card-title-[^"]*)"', html)
    targets = re.findall(r'hx-target="#(card-title-[^"]*)"', html)

    assert len(boxes) == len(items) == len(targets)
    assert len(set(boxes)) == len(items), "the title ids must differ"
    assert boxes == targets, "each button must swap its own title"
    # ...and the fragment address of each is its own tender, not the tender of the upper card.
    for it, box in zip(items, boxes):
        assert box == f"card-title-feed-{it.tender_id}-{it.profile_id}"
        assert f"/app/tenders/{it.tender_id}/title?profile_id={it.profile_id}" in html


def test_teaser_cards_also_get_unique_self_referential_ids():
    """The dashboard teaser — three cards on the same partial, the same invariant."""
    import re

    from src.api.templating import templates

    items = _multi_items(3)
    html = templates.get_template("_cockpit_feed_teaser.html").render(
        feed_items=items, profiles=[object()], telegram_linked=True
    )

    boxes = re.findall(r'id="(card-title-[^"]*)"', html)
    targets = re.findall(r'hx-target="#(card-title-[^"]*)"', html)
    assert len(boxes) == 3 == len(set(boxes))
    assert boxes == targets


def test_second_cards_fragment_returns_its_own_translated_title(monkeypatch):
    """The request of the fragment for the SECOND card translates its title, not the first one’s."""
    second = _multi_items(2)[1]
    monkeypatch.setattr(
        routes_tender_ai.svc,
        "get_tender_analyses",
        _async(
            TenderAnalyses(
                tender_id=second.tender_id,
                profile_id=_PROFILE,
                profile_name="ИТ набавке",
                title="Намештај",
                customer_name=None,
                deadline_at=None,
                source_url=second.tender_url,
                outputs={},
                description=_DESC_SR,
            )
        ),
    )
    spy = _TranslateSpy(TenderTranslation(title="Мебель", description=""))
    monkeypatch.setattr(routes_tender_ai.translation, "translate_tender", spy)

    resp = _client().get(
        f"/app/tenders/{second.tender_id}/title?profile_id={_PROFILE}&lang=ru"
    )

    assert resp.status_code == 200
    assert "Мебель" in resp.text
    assert f'id="card-title-feed-{second.tender_id}-{_PROFILE}"' in resp.text
    assert spy.calls[0]["tender_id"] == second.tender_id
    assert spy.calls[0]["description"] is None


def test_cards_never_call_the_model_on_render():
    """‼️ Cost under control: translation — only on a click.

    A guard against “let’s translate the whole feed at once”: the feed services have no
    right to call translate_tender, otherwise one visit to /app/feed = 20 model calls.
    """
    from pathlib import Path

    for module in ("feed_service.py", "cockpit_service.py"):
        path = Path("src/api/cabinet") / module
        if path.exists():
            assert "translate_tender" not in path.read_text(encoding="utf-8")

    for tpl in ("_feed_card.html", "_cockpit_feed_teaser.html"):
        text = Path("src/api/templates") / tpl
        assert "show_original=true" in text.read_text(encoding="utf-8")
