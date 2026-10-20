"""Common fixtures of the bot tests.

The bot takes the sources of a new profile from the ``sources`` registry
(:func:`src.domain.sources.default_source_keys_for_new_profile`, SRC-0a). The bot tests mock ``session.execute`` with a strict queue of responses, and an extra SELECT
would shift it — therefore the registry is substituted here, at the level of the bot module,
by the production fact of 30.09.2026: exactly jnportal (RS) is enabled. That the bot really takes
the keys from the registry, and not a literal, is pinned by a separate test
(``test_telegram_fsm.py::test_onboarding_takes_sources_from_registry``).
"""

from __future__ import annotations

import pytest

import src.bots.telegram as telegram
from src.domain.sources import SourceInfo


@pytest.fixture(autouse=True)
def _registry_sources(monkeypatch):
    async def _default(session):
        return ["jnportal"]

    monkeypatch.setattr(telegram, "default_source_keys_for_new_profile", _default)

    # /profile and /izvori read the registry for the platform labels (SRC-0b) — the same
    # production fact; the /izvori tests substitute the list themselves.
    async def _selection(session, selected=()):
        return [
            SourceInfo(
                source_key="jnportal",
                display_name="Portal javnih nabavki (UJN)",
                country_codes=("RS",),
                available_for_tiers=None,
            )
        ]

    monkeypatch.setattr(telegram, "list_sources_for_selection", _selection)
