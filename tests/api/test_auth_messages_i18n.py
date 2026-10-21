"""The responses of the registration form — in the client’s language (not always in Russian).

Defect: the ``signup`` messages (“Enter a correct email”, “The 'I am not a robot' check failed”,
“Trial has already been activated”) were hard-coded in Russian right inside ``auth_service``,
although the locale was ALREADY resolved in the function (``lang =
norm_locale(locale)``) and the language of the email was chosen by it nearby. Registration is the client’s first
contact: a Serb got a refusal in a foreign language.

The guard below holds the invariant with a predicate (an ast scan of the returned literals), not by eye:
the next refusal, added as a Russian line, will turn red by itself.
"""

from __future__ import annotations

import ast
import pathlib
import re

import pytest

from src.api.cabinet import auth_service as svc
from src.api.public.i18n import AUTH_MSG, auth_message
from tests.api.test_auth import (
    FakeSession,
    MockEmailSender,
    _settings,
    bad_turnstile,
    disposable_email,
    good_email,
    ok_turnstile,
)

_CYR = re.compile(r"[а-яА-ЯёЁ]")


def test_auth_msg_keys_parity():
    """The AUTH_MSG keys match in all three languages."""
    keys = {lang: set(msgs) for lang, msgs in AUTH_MSG.items()}
    assert set(keys) == {"sr", "ru", "en"}
    assert keys["sr"] == keys["ru"] == keys["en"], keys


def test_auth_message_unknown_locale_falls_back_to_sr():
    """Owner’s decision 30.09.2026: an unknown/empty language → sr (not ru)."""
    assert auth_message(None, "invalid_email") == AUTH_MSG["sr"]["invalid_email"]
    assert auth_message("de", "invalid_email") == AUTH_MSG["sr"]["invalid_email"]
    assert auth_message("ru", "invalid_email") == AUTH_MSG["ru"]["invalid_email"]


@pytest.mark.parametrize("locale", ["sr", "en"])
@pytest.mark.asyncio
async def test_signup_rejection_speaks_client_language(locale):
    """The Turnstile refusal arrives in the client’s language, without Russian letters.

    Mutation: return a literal instead of ``auth_message(lang, …)`` → the test turns red.
    """
    result = await svc.signup(
        FakeSession(),
        email="user@company.rs",
        turnstile_token="bad",
        turnstile=bad_turnstile(),
        email_validator=good_email(),
        email_sender=MockEmailSender(),
        settings=_settings(),
        locale=locale,
    )
    assert result.outcome == svc.REJECTED_TURNSTILE
    assert not _CYR.search(result.message or ""), result.message
    assert result.message == AUTH_MSG[locale]["turnstile_failed"]


@pytest.mark.asyncio
async def test_signup_rejection_keeps_russian_for_ru_client():
    """A Russian client sees the former text — the translations do not replace the canon."""
    result = await svc.signup(
        FakeSession(),
        email="x@mailinator.com",
        turnstile_token="ok",
        turnstile=ok_turnstile(),
        email_validator=disposable_email(),
        email_sender=MockEmailSender(),
        settings=_settings(),
        locale="ru",
    )
    assert result.outcome == svc.REJECTED_EMAIL
    assert result.message == AUTH_MSG["ru"]["email_rejected"]


def test_no_russian_literals_returned_from_signup():
    """Guard: ``signup`` returns no Russian literals — only the catalog.

    A predicate, not a list: we walk the ast of the function and look for Cyrillic string
    constants in ``return``/arguments — a new refusal added in Russian
    will turn red without editing the test.
    """
    src = pathlib.Path(svc.__file__).read_text(encoding="utf-8")
    tree = ast.parse(src)
    fn = next(
        n
        for n in ast.walk(tree)
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == "signup"
    )
    doc = ast.get_docstring(fn, clean=False)
    leaks = [
        node.value
        for node in ast.walk(fn)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and node.value != doc
        and _CYR.search(node.value)
    ]
    assert (
        not leaks
    ), f"Russian literals in signup (they must be in the catalog): {leaks}"
