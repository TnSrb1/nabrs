"""Common fixtures of the tests.

``_legacy_ru_default`` — the TRANSITIONAL bridge of the owner’s decision (30.09.2026: Serbian is the default language everywhere). Hundreds of old tests render the cabinet/letters/the bot WITHOUT a locale and check the Russian literals; their subject is not the language but the logic. So as not to rewrite them all at once, the fixture returns them the previous default ``ru``.

The real default ``sr`` is checked by the tests with the marker ``@pytest.mark.sr_default``
(or the module-level ``pytestmark``): for them the fixture substitutes nothing.
New tests about the language — only with this marker and with literals from the fact.
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _legacy_ru_default(request, monkeypatch):
    if request.node.get_closest_marker("sr_default"):
        return
    from src.api import templating
    from src.api.cabinet import i18n as cabinet_i18n
    import src.i18n_email as i18n_email

    monkeypatch.setattr(i18n_email, "DEFAULT", "ru")
    monkeypatch.setattr(cabinet_i18n, "DEFAULT", "ru")
    env = templating.templates.env.globals
    monkeypatch.setitem(
        env,
        "t",
        {**cabinet_i18n.COMMON["ru"], **cabinet_i18n.CABINET["ru"]},
    )
    monkeypatch.setitem(env, "lang", "ru")
