"""Choice of sources on “Composition”, on the “as is” screen and in the template card.

The ``sources`` registry and the preview are mocked (ADR-013), templates render for real. Expectations — LITERALS from the fact (markup and catalog), not via ``t()``.
"""

from __future__ import annotations

from src.api.cabinet import routes_library, source_choices
from src.domain import sources as domain_sources
from src.domain.sources import SourceInfo
from tests.api.test_cabinet_i18n import _TID, _UID, _async, _client

# iat is mandatory: without it csrf_token is empty and any form is refused (this is a rule).
SESS = {"uid": _UID, "tid": _TID, "iat": 1_700_000_000}

JN = SourceInfo("jnportal", "JN", ("RS",), None, enabled=True)
CEJN = SourceInfo("cejn", "CEJN", ("ME",), None, enabled=True)
NIS = SourceInfo("nis", "NIS", ("RS",), None, enabled=True)
ALL_ENABLED = [JN, CEJN, NIS]


def _registry(monkeypatch, sources=ALL_ENABLED):
    """The registry: both the captions for the checkboxes and the check of the submitted keys."""

    async def _selection(session, selected=()):
        known = {s.source_key for s in sources}
        extra = [
            SourceInfo(k, "", (), None, enabled=False)
            for k in selected
            if k not in known
        ]
        return sorted([*sources, *extra], key=lambda s: s.source_key)

    monkeypatch.setattr(source_choices, "list_sources_for_selection", _selection)
    monkeypatch.setattr(domain_sources, "list_enabled_sources", _async(sources))


def _stub_preview(monkeypatch):
    from src.api.cabinet import preview_service

    async def _stub(db, payload, examples_limit=0):
        return preview_service.PreviewResult(count=0, unfound=[], examples=[])

    monkeypatch.setattr(routes_library.preview_service, "preview_count", _stub)


def _tier(monkeypatch, tier=("pro", "active"), used=(1,)):
    monkeypatch.setattr(
        routes_library.subscription_service, "get_tenant_tier", _async(tier)
    )
    monkeypatch.setattr(
        routes_library.profile_service, "list_profiles", _async(list(used))
    )


# --- 1. “Composition”: source checkboxes, RS ticked, hint -------------------- #


def test_library_detail_marks_rs_sources_and_leaves_foreign_unchecked(monkeypatch):
    """The RS source is ticked by default, ME — not (as for a new profile)."""
    _registry(monkeypatch)
    _stub_preview(monkeypatch)
    _tier(monkeypatch)
    monkeypatch.setattr(
        routes_library, "default_source_keys_for_new_profile", _async(["jnportal"])
    )
    html = _client(sess=SESS, locale="sr").get("/library/package/remote").text
    assert (
        '<input type="checkbox" name="enabled_source_keys" value="jnportal" checked>'
        in html
    )
    assert '<input type="checkbox" name="enabled_source_keys" value="cejn">' in html
    assert "Dodajte da dobijate tendere iz BiH, Crne Gore, S. Makedonije" in html


def test_library_detail_both_buttons_carry_selected_sources(monkeypatch):
    """Both buttons — the submit of one GET form: “as is” changes the address through formaction."""
    _registry(monkeypatch)
    _stub_preview(monkeypatch)
    _tier(monkeypatch)
    monkeypatch.setattr(
        routes_library, "default_source_keys_for_new_profile", _async(["jnportal"])
    )
    html = _client(sess=SESS, locale="sr").get("/library/package/remote").text
    assert '<form method="get" action="/profiles/new"' in html
    assert '<input type="hidden" name="src_sent" value="1">' in html
    assert 'formaction="/profiles/new/confirm"' in html
    # Without JS (CSP): not a single inline handler on the composition buttons.
    assert "onclick=" not in html


def test_library_detail_limit_reached_shows_no_source_form(monkeypatch):
    """The limit is exhausted — we lead to the account, we do not offer the choice of sources."""
    _registry(monkeypatch)
    _stub_preview(monkeypatch)
    _tier(monkeypatch, used=(1, 2, 3))
    monkeypatch.setattr(
        routes_library, "default_source_keys_for_new_profile", _async(["jnportal"])
    )
    html = _client(sess=SESS, locale="sr").get("/library/package/remote").text
    assert 'name="enabled_source_keys"' not in html
    assert 'href="/cabinet/account"' in html


# --- 2. “as is”: checkboxes in a POST form, the choice goes to POST /profiles ------- #


def _confirm_stubs(monkeypatch):
    from src.api.cabinet import preview_service, routes_profiles

    async def _stub(db, payload, examples_limit=0):
        return preview_service.PreviewResult(count=0, unfound=[], examples=[])

    monkeypatch.setattr(routes_profiles.preview_service, "preview_count", _stub)
    monkeypatch.setattr(
        routes_profiles.channel_svc, "live_delivery_channels", _async([])
    )


def _confirm_form(html: str) -> str:
    form = html[html.index('<form method="post" action="/profiles" id="confirmForm"') :]
    return form[: form.index("</form>")]


def test_confirm_keeps_picked_sources_checked_and_drops_unknown(monkeypatch):
    """The choice from “Composition” reaches the screen; a forged key `evil` is discarded."""
    _registry(monkeypatch)
    _confirm_stubs(monkeypatch)
    url = (
        "/profiles/new/confirm?from_package=gradjevina"
        "&enabled_source_keys=jnportal&enabled_source_keys=cejn"
        "&enabled_source_keys=evil"
    )
    html = _client(sess=SESS, locale="sr").get(url).text
    assert (
        '<input type="checkbox" name="enabled_source_keys" value="cejn" checked>'
        in html
    )
    assert (
        '<input type="checkbox" name="enabled_source_keys" value="jnportal" checked>'
        in html
    )
    # There is no checkbox with the key evil (the string may remain in the language-switch link — there is query).
    assert 'value="evil"' not in html


def test_confirm_checkboxes_are_inside_the_post_form(monkeypatch):
    """The checkboxes go to POST /profiles (the same name contract as the full form)."""
    _registry(monkeypatch)
    _confirm_stubs(monkeypatch)
    html = (
        _client(sess=SESS, locale="sr")
        .get(
            "/profiles/new/confirm?from_package=gradjevina&enabled_source_keys=cejn&src_sent=1"
        )
        .text
    )
    form = _confirm_form(html)
    assert 'value="cejn" checked>' in form
    # The “empty” sentinel always goes: all ticks removed — an error, not a default.
    assert '<input type="hidden" name="enabled_source_keys" value="">' in form


def test_confirm_empty_selection_shows_error_at_field(monkeypatch):
    """All ticks removed (src_sent without keys) — an error at the field, the default RS is not substituted."""
    _registry(monkeypatch)
    _confirm_stubs(monkeypatch)
    html = (
        _client(sess=SESS, locale="sr")
        .get("/profiles/new/confirm?from_package=gradjevina&src_sent=1")
        .text
    )
    assert 'role="alert">Izaberite bar jedan izvor' in html
    assert 'value="jnportal" checked>' not in html


def test_confirm_without_picks_keeps_rs_default(monkeypatch):
    """Without parameters (a direct transition from the showcase) — the default as before: RS ticked."""
    _registry(monkeypatch)
    _confirm_stubs(monkeypatch)
    html = (
        _client(sess=SESS, locale="sr")
        .get("/profiles/new/confirm?from_package=gradjevina")
        .text
    )
    assert 'value="jnportal" checked>' in html
    assert 'value="nis" checked>' in html
    assert 'value="cejn" checked>' not in html


# --- 3. The template card: “+ source” — ONE source into THIS template ---------- #

PID = "p-1"
NOW_ISO = "2026-10-17T10:00:00+00:00"


def _tpl(keys, opt=None):
    from datetime import datetime, timezone
    from types import SimpleNamespace

    return SimpleNamespace(
        id=PID,
        name="Стройка",
        is_active=True,
        match_all=False,
        regions=[],
        contract_types=[],
        customers_include=[],
        customers_exclude=[],
        delivery_channels=[],
        delivery_schedule="immediate",
        ai_analysis_level="none",
        enabled_source_keys=list(keys),
        source_opt_in=dict(opt or {}),
        deleted_at=None,
        created_at=datetime(2026, 10, 1, tzinfo=timezone.utc),
        min_amount_eur=None,
        max_amount_eur=None,
        min_days_until_deadline=None,
        include_no_amount=False,
        keywords_include=[],
        keywords_exclude=[],
        keyword_dict=False,
        classifications={},
        debug_match=False,
    )


def _card_stubs(monkeypatch, tpl):
    from src.api import security
    from src.api.cabinet import routes_profiles
    from src.api.config import get_cabinet_settings

    monkeypatch.setattr(routes_profiles.svc, "get_profile", _async(tpl))
    monkeypatch.setattr(routes_profiles.svc, "downgrade_notice", _async(None))
    monkeypatch.setattr(routes_profiles.svc, "list_profiles", _async([tpl]))
    monkeypatch.setattr(routes_profiles.channel_svc, "delivery_warning", _async(None))
    secret = get_cabinet_settings().cabinet_secret_key
    return security.csrf_token(SESS, secret)


def test_card_shows_missing_enabled_source_as_add_button(monkeypatch):
    """The template has only jnportal → cejn (enabled, not standing) is offered by a button."""
    _registry(monkeypatch)
    tpl = _tpl(["jnportal"], {"jnportal": NOW_ISO})
    _card_stubs(monkeypatch, tpl)
    html = _client(sess=SESS, locale="sr").get(f"/profiles/{PID}").text
    assert f'action="/profiles/{PID}/sources/add"' in html
    assert '<input type="hidden" name="source_key" value="cejn">' in html
    assert '<input type="hidden" name="source_key" value="nis">' in html
    # An already standing source does not get into the “+” buttons.
    assert '<input type="hidden" name="source_key" value="jnportal">' not in html


def test_add_puts_one_source_into_this_template_with_opt_in_moment(monkeypatch):
    """POST with CSRF: cejn is added to this template, the subscription moment — ``now``."""
    _registry(monkeypatch)
    tpl = _tpl(["jnportal"], {"jnportal": NOW_ISO})
    token = _card_stubs(monkeypatch, tpl)
    r = _client(sess=SESS, locale="sr").post(
        f"/profiles/{PID}/sources/add", data={"csrf": token, "source_key": "cejn"}
    )
    assert r.status_code == 303
    assert r.headers["location"] == f"/profiles/{PID}"
    assert tpl.enabled_source_keys == ["jnportal", "cejn"]
    assert "cejn" in tpl.source_opt_in and tpl.source_opt_in["cejn"] != NOW_ISO
    # The former moment of jnportal is not touched.
    assert tpl.source_opt_in["jnportal"] == NOW_ISO


def test_add_twice_creates_no_duplicate_and_keeps_moment(monkeypatch):
    """A repeated press — without a duplicate and without a shift of the subscription moment."""
    _registry(monkeypatch)
    tpl = _tpl(
        ["jnportal", "cejn"], {"jnportal": NOW_ISO, "cejn": "2026-10-02T08:00:00+00:00"}
    )
    token = _card_stubs(monkeypatch, tpl)
    _client(sess=SESS, locale="sr").post(
        f"/profiles/{PID}/sources/add", data={"csrf": token, "source_key": "cejn"}
    )
    assert tpl.enabled_source_keys == ["jnportal", "cejn"]
    assert tpl.source_opt_in["cejn"] == "2026-10-02T08:00:00+00:00"


def test_add_without_csrf_writes_nothing(monkeypatch):
    _registry(monkeypatch)
    tpl = _tpl(["jnportal"], {"jnportal": NOW_ISO})
    _card_stubs(monkeypatch, tpl)
    r = _client(sess=SESS, locale="sr").post(
        f"/profiles/{PID}/sources/add", data={"csrf": "nope", "source_key": "cejn"}
    )
    assert r.status_code == 303
    assert tpl.enabled_source_keys == ["jnportal"]
    assert tpl.source_opt_in == {"jnportal": NOW_ISO}


def test_add_unknown_source_key_is_refused(monkeypatch):
    """A key outside the registry (forged by hand) — past the record, as is a disabled source."""
    _registry(monkeypatch)
    tpl = _tpl(["jnportal"], {"jnportal": NOW_ISO})
    token = _card_stubs(monkeypatch, tpl)
    _client(sess=SESS, locale="sr").post(
        f"/profiles/{PID}/sources/add", data={"csrf": token, "source_key": "evil"}
    )
    assert tpl.enabled_source_keys == ["jnportal"]


def test_add_foreign_or_missing_profile_is_404(monkeypatch):
    """A foreign tenant / a deleted template: get_profile does not find it — 404, without a record."""
    from src.api.cabinet import routes_profiles

    _registry(monkeypatch)
    token = _card_stubs(monkeypatch, None)
    monkeypatch.setattr(routes_profiles.svc, "get_profile", _async(None))
    r = _client(sess=SESS, locale="sr").post(
        f"/profiles/{PID}/sources/add", data={"csrf": token, "source_key": "cejn"}
    )
    assert r.status_code == 404


def test_add_htmx_returns_block_fragment(monkeypatch):
    """An htmx request gets the updated block (#pd-sources), not a redirect."""
    _registry(monkeypatch)
    tpl = _tpl(["jnportal"], {"jnportal": NOW_ISO})
    token = _card_stubs(monkeypatch, tpl)
    r = _client(sess=SESS, locale="sr").post(
        f"/profiles/{PID}/sources/add",
        data={"csrf": token, "source_key": "cejn"},
        headers={"HX-Request": "true"},
    )
    assert r.status_code == 200
    assert 'id="pd-sources"' in r.text
    # After adding, cejn is already there — it is not in the buttons.
    assert '<input type="hidden" name="source_key" value="cejn">' not in r.text
