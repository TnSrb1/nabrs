"""The operator edits the showcase translations of templates in the same place where they edit the Russian text.

Owner’s requirement (brief of 23.07): language variants are stored in the DB precisely
“so that texts can later be edited from the admin panel, not only by editing the code”.
Migration 0036 and the locale-based reading arrived in #450, but the moderation form kept
a single Russian field — this item is closed here.

The admin panel remains a ru interface (admin i18n is out of scope) — this is only about the
translation INPUT fields.
"""

from __future__ import annotations

import pathlib
from types import SimpleNamespace

import pytest

from src.api.cabinet import template_library_service as svc


class _FakeResult:
    def __init__(self, obj):
        self._obj = obj

    def scalar_one_or_none(self):
        return self._obj


class _FakeDb:
    def __init__(self, obj):
        self._obj = obj
        self.flushed = False

    async def execute(self, *_a, **_kw):
        return _FakeResult(self._obj)

    async def flush(self):
        self.flushed = True

    async def commit(self):
        self.flushed = True


def _tpl():
    return SimpleNamespace(
        id="11111111-1111-1111-1111-111111111111",
        name="Строительство",
        description="Общестроительные работы",
        activity_kind="Строительство",
        status="pending",
        i18n=None,
        updated_at=None,
        keywords_include=[{"all_of": ["строительство"]}],
        classifications=None,
    )


@pytest.mark.asyncio
async def test_operator_can_save_translations():
    """Translations are saved into i18n in the same form as the seeded templates.

    Mutation: remove the ``i18n`` handling in ``update_pending`` → the test turns red.
    """
    tpl = _tpl()
    ok = await svc.update_pending(
        _FakeDb(tpl),
        admin_uid="admin-1",
        template_id=str(tpl.id),
        name="Строительство",
        description="Общестроительные работы",
        i18n={
            "sr": {"name": "Građevina", "description": "Opšti radovi"},
            "en": {"name": "Construction", "description": "General works"},
        },
    )
    assert ok is True
    assert tpl.i18n["sr"]["name"] == "Građevina"
    assert tpl.i18n["en"]["description"] == "General works"


@pytest.mark.asyncio
async def test_empty_translation_is_not_stored_as_blank():
    """An empty field means “no translation”, not an empty string.

    Otherwise the showcase would display an empty title instead of falling back to Russian.
    """
    tpl = _tpl()
    await svc.update_pending(
        _FakeDb(tpl),
        admin_uid="admin-1",
        template_id=str(tpl.id),
        name="Строительство",
        description=None,
        i18n={
            "sr": {"name": "", "description": ""},
            "en": {"name": "", "description": ""},
        },
    )
    assert tpl.i18n is None, tpl.i18n


@pytest.mark.asyncio
async def test_partial_translation_keeps_only_filled_fields():
    """One field filled — we save it, the rest stays on the fallback."""
    tpl = _tpl()
    await svc.update_pending(
        _FakeDb(tpl),
        admin_uid="admin-1",
        template_id=str(tpl.id),
        name="Строительство",
        description="Общестроительные работы",
        i18n={
            "sr": {"name": "Građevina", "description": ""},
            "en": {"name": "", "description": ""},
        },
    )
    assert tpl.i18n == {"sr": {"name": "Građevina"}}
    view = svc.localized_view(tpl, "sr")
    assert view["name"] == "Građevina"
    # The description has no translation → we show the Russian original, not emptiness.
    assert view["description"] == "Общестроительные работы"


@pytest.mark.asyncio
async def test_none_i18n_does_not_wipe_existing_translations():
    """An edit without the translations block does not erase the already saved ones.

    The route always sends the dictionary, but the service is also called from other places —
    the contract must be safe by default.
    """
    tpl = _tpl()
    tpl.i18n = {"sr": {"name": "Građevina"}}
    await svc.update_pending(
        _FakeDb(tpl),
        admin_uid="admin-1",
        template_id=str(tpl.id),
        name="Строительство (правка)",
        description=None,
    )
    assert tpl.i18n == {"sr": {"name": "Građevina"}}
    assert tpl.name == "Строительство (правка)"


def test_moderation_form_declares_translation_inputs():
    """The moderation form declares translation input fields for sr and en.

    ‼️ We check the DECLARATION in the template, not a rendering of a fragment: the first version of the test
    rendered the cut-out block through its own Jinja Environment — it passed locally,
    but failed in the full environment (a different jinja2 version / the block was parsed differently).
    A brittle test is worse than none: it turns red where the defect is not.

    The “form → route” link is caught by a neighbouring test: the route reads the same field names.
    """
    html = pathlib.Path("src/api/templates/admin_moderation.html").read_text(
        encoding="utf-8"
    )
    assert '("sr", "Сербский"), ("en", "Английский")' in html, "no loop over languages"
    assert 'name="name_{{ code }}"' in html
    assert 'name="description_{{ code }}"' in html
    # The saved translation is put into the field and is not lost on edit.
    assert "tr.get('name')" in html and "tr.get('description')" in html


def test_admin_route_reads_translation_fields():
    """The route collects translations from the SAME field names that are declared in the form."""
    src = pathlib.Path("src/api/admin/routes_admin.py").read_text(encoding="utf-8")
    # We check the field NAMES, not the way they are read: the first version asserted
    # `form.get(f"name_{lang}")` and turned red from a harmless refactor
    # (moving it into a helper for typing). The test must hold the contract, not the code.
    assert 'f"name_{lang}"' in src
    assert 'f"description_{lang}"' in src
    assert 'for lang in ("sr", "en")' in src
    assert "i18n=i18n" in src, "the collected translations are not passed to the service"
