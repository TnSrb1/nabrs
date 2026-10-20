"""The owner’s decision 30.09.2026: Serbian (Latin) — the default language everywhere.

All the assertions — literals from the fact, not through ``t()``.
"""

import pytest

from src.api.public.i18n import resolve_lang
from src.bots.i18n import bot_t
from src.i18n_email import email_t, norm_locale

pytestmark = pytest.mark.sr_default


class _Req:
    def __init__(self, query=None, cookies=None, accept=None):
        self.query_params = query or {}
        self.cookies = cookies or {}
        self.headers = {"accept-language": accept} if accept else {}


@pytest.mark.parametrize("accept", [None, "en-US,en;q=0.9", "ru-RU,ru;q=0.9"])
def test_public_resolver_ignores_accept_language(accept):
    assert resolve_lang(_Req(accept=accept)) == "sr"


def test_public_resolver_explicit_choice_wins():
    assert resolve_lang(_Req(query={"lang": "ru"}, accept="en")) == "ru"
    assert resolve_lang(_Req(cookies={"lang": "en"}, accept="ru")) == "en"


@pytest.mark.parametrize("loc", [None, "", "de", "xx"])
def test_norm_locale_falls_back_to_sr(loc):
    assert norm_locale(loc) == "sr"


def test_email_and_bot_chrome_default_to_serbian():
    assert email_t(None, "greeting") == "Zdravo!"
    assert bot_t(None, "unwatch_ok") == bot_t("sr", "unwatch_ok")
    assert bot_t(None, "unwatch_ok") != bot_t("ru", "unwatch_ok")


def test_existing_ru_users_keep_ru():
    assert norm_locale("ru") == "ru"


class _NoTenantSession:
    async def execute(self, *a, **kw):
        return type("R", (), {"scalar": lambda self: None})()


async def test_bot_start_without_account_resolves_serbian():
    """A chat without an account (`/start` before registration) → sr, not ru."""
    from src.bots.telegram import resolve_bot_locale

    assert await resolve_bot_locale(_NoTenantSession(), 12345) == "sr"
