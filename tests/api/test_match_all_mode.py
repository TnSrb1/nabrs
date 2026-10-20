"""The profile mode “All tenders” (0043): the whole stream — only by an EXPLICIT choice.

Before 0043 “show everything” was not a mode — it followed from the EMPTINESS of the profile: in ``STAGE_A_SQL`` all the branches “filter not set” are true, and ``stage_b_filter`` without include groups returns ``True``. Therefore a client who abandoned the form halfway got a mirror of the portal and could not understand why. The production measurement 15.08.2026 over 7 days: 813 + 813 messages to two service profiles and 470 to a live subscriber whose profile was named “CLIENT_PROFILE” but contains neither a keyword, nor CPV, nor a district.

The tests pin three sides of one contract:

1. routing (``STAGE_A_SQL``) — without a topic and without ``match_all`` the profile does not match;
2. the preview and the showcase counters — the same rule, otherwise the form promises tenders to a profile that will not get a single e-mail (the CLAUDE.md invariant “a predicate that duplicates the selection of another module — mirror it”);
3. parsing of the form — in the “All tenders” mode the AI is switched off FORCIBLY: ~170 announcements a day through the model is direct money.
"""

from __future__ import annotations

import re

import pytest

from src.api import forms
from src.api.cabinet import preview_service, template_prefill
from src.workers.matching import STAGE_A_SQL

_ROUTING_SQL = str(STAGE_A_SQL)


# --------------------------------------------------------------------------- #
#  1. Routing: the branch of the mode in stage A
# --------------------------------------------------------------------------- #


def test_stage_a_requires_match_all_or_topic():
    """STAGE_A_SQL has a branch “match_all OR topic”.

    The expectation is taken from a FACT in production (a profile without a topic received the whole stream), not from the model of the code. We check the link as a whole: one mention of match_all is not enough — what matters is that the alternatives are keywords and classifications, not regions/amounts (those only narrow and do not stop being a mirror of the portal).
    """
    branch = re.search(
        r"AND \(\s*p\.match_all = true(.*?)\n      \)", _ROUTING_SQL, flags=re.S
    )
    assert branch, "no match_all branch in STAGE_A_SQL — a profile without a topic sends everything again"
    body = branch.group(1)
    assert "keywords_include" in body
    assert "classifications->'include'" in body
    assert "regions" not in body, (
        "a region does not set a topic: a profile “the whole portal, but in seven districts” — "
        "the same mirror, it must not pass through this branch"
    )


# --------------------------------------------------------------------------- #
#  2. Mirror: the preview answers the same way as routing
# --------------------------------------------------------------------------- #


class _ExplodingDB:
    """A session that fails on any request — proves that the DB was not reached."""

    async def execute(self, *args, **kwargs):  # pragma: no cover - must not be called
        raise AssertionError("the preview must not go to the DB for a profile without a topic")


@pytest.mark.asyncio
async def test_preview_is_zero_for_profile_without_topic_and_flag():
    payload = template_prefill.base_payload()
    result = await preview_service.preview_count(_ExplodingDB(), payload)
    assert result.count == 0


def test_matches_anything_mirrors_stage_a_axes():
    """Topic = words OR classifications; the narrowing axes do not count as a topic."""
    # A source is set (as for any saved profile, SRC-0b) — the topic axes
    # checked with it; without a source the answer is “no” for ANY topic (below).
    empty = template_prefill.base_payload(source_keys=["jnportal"])
    assert preview_service.matches_anything(empty) is False
    no_source = template_prefill.base_payload()
    assert preview_service.matches_anything({**no_source, "match_all": True}) is False
    assert (
        preview_service.matches_anything(
            {**no_source, "keywords_include": [{"all_of": ["asfalt"]}]}
        )
        is False
    )

    assert preview_service.matches_anything({**empty, "match_all": True}) is True
    assert (
        preview_service.matches_anything(
            {**empty, "keywords_include": [{"all_of": ["asfalt"]}]}
        )
        is True
    )
    assert (
        preview_service.matches_anything(
            {**empty, "classifications": {"include": [{"code": "45*"}], "exclude": []}}
        )
        is True
    )
    # An empty group is not a topic: the form returns [] for an empty input string.
    assert (
        preview_service.matches_anything(
            {**empty, "keywords_include": [{"all_of": []}]}
        )
        is False
    )
    # Districts/amounts narrow, but the profile stays a mirror of the portal.
    assert (
        preview_service.matches_anything(
            {**empty, "regions": ["okrug-1"], "min_amount_eur": 0}
        )
        is False
    )


# --------------------------------------------------------------------------- #
#  3. The form: the mode is declared explicitly and switches the AI off
# --------------------------------------------------------------------------- #


def _form(**over) -> dict:
    base = {
        "name": "тест",
        "delivery_schedule": "immediate",
        "ai_analysis_level": "deep",
    }
    base.update(over)
    return base


def test_match_all_forces_ai_off():
    """The expensive AI level in the “whole stream” mode is switched off at the PARSING of the form.

    Not by a hint in the markup: the markup can be bypassed, and ~170 analyzes a day is unlimited spend for whoever simply pressed “Show all”.
    """
    payload, errors = forms.parse_profile_form(_form(match_all="on"))
    assert payload["match_all"] is True
    assert payload["ai_analysis_level"] == "none"
    assert errors == []


def test_ai_level_is_always_off_after_auto_analysis_removal():
    """The auto-analysis of the stream is removed (0044): the form always writes ``none``.

    This test used to pin the reverse — that the level “stays selected by the client”. The contract is changed by the owner’s decision: AI is started by a person with a button in the tender card, and there is no longer a subscription to analyze the whole stream. The value from the request is ignored on purpose — there is no field in the form, but a POST can be sent by hand, and a paid call enabled by forging the form is a hole.
    """
    payload, errors = forms.parse_profile_form(_form(ai_analysis_level="deep"))
    assert payload["ai_analysis_level"] == "none"
    assert errors == []


def test_unfiltered_preset_declares_the_mode():
    """The card “Without a filter — show all” leads to the form with the mode switched on."""
    assert template_prefill.unfiltered_payload()["match_all"] is True
    assert template_prefill.base_payload()["match_all"] is False


# --------------------------------------------------------------------------- #
#  4. A silent profile explains itself to the client
# --------------------------------------------------------------------------- #


def _dashboard_client():
    from src.api import security
    from src.api.config import get_cabinet_settings
    from src.api.main import app
    from fastapi.testclient import TestClient
    from src.api import deps

    sess = {"uid": "u-1", "tid": "t-1"}

    def factory():
        from tests.api.conftest import FakeSession

        return FakeSession()

    app.dependency_overrides[deps.get_session_factory] = lambda: factory
    app.dependency_overrides[deps.get_optional_session] = lambda: sess
    client = TestClient(app, follow_redirects=False)
    settings = get_cabinet_settings()
    client.cookies.set(
        "tb_session",
        security.sign_session(sess["uid"], sess["tid"], settings.cabinet_secret_key),
    )
    return client


def _stub_dashboard(monkeypatch, profiles):
    from src.api.cabinet import routes_profiles

    async def _ret(*args, **kwargs):
        return profiles

    async def _empty(*args, **kwargs):
        return []

    async def _false(*args, **kwargs):
        return False

    async def _status(*args, **kwargs):
        return {"has_live_channel": False, "gaps": [], "channelless": []}

    monkeypatch.setattr(routes_profiles.svc, "list_profiles", _ret)
    monkeypatch.setattr(routes_profiles.svc, "downgrade_notice", _empty)
    monkeypatch.setattr(routes_profiles.channel_svc, "has_active_telegram", _false)
    monkeypatch.setattr(routes_profiles.channel_svc, "delivery_channel_status", _status)


def test_dashboard_warns_about_a_profile_that_finds_nothing(monkeypatch):
    """A profile without a topic and without the mode is silent — the client must learn about it FROM US.

    Otherwise they see only “no deliveries” and read it as “no tenders”: exactly the state from which the whole 0043 change grew.
    """
    _stub_dashboard(
        monkeypatch,
        [{"id": "p-1", "match_all": False, "enabled_source_keys": ["jnportal"]}],
    )
    resp = _dashboard_client().get("/app")
    assert resp.status_code == 200
    assert "profile-silent" in resp.text
    assert "/profiles/p-1/edit" in resp.text


def test_dashboard_is_silent_about_profiles_that_do_match(monkeypatch):
    """Counter-fact: the mode is on — there is nothing to warn about."""
    _stub_dashboard(
        monkeypatch,
        [{"id": "p-2", "match_all": True, "enabled_source_keys": ["jnportal"]}],
    )
    resp = _dashboard_client().get("/app")
    assert resp.status_code == 200
    assert "profile-silent" not in resp.text


def test_dashboard_warns_about_a_profile_without_a_source(monkeypatch):
    """The source is an entry, not a filter: without it the profile is dead in any mode.

    Production 15.08: the profile “Video surveillance, SKS” of a live client has
    `enabled_source_keys = {}` — not a single tender, although the “All tenders” mode is on. `= ANY(COALESCE(p.enabled_source_keys, '{}'))` in STAGE_A_SQL has no leniency for “filter not set”.
    """
    _stub_dashboard(
        monkeypatch, [{"id": "p-3", "match_all": True, "enabled_source_keys": []}]
    )
    resp = _dashboard_client().get("/app")
    assert resp.status_code == 200
    assert "profile-silent" in resp.text
