"""The configuration of the bots (pydantic-settings).

There is no separate config module in the project yet (the API skeleton is minimal), so
we create a minimal :class:`BotSettings` here. The values are read from ENV;
the secret (``TG_BOT_TOKEN``) is NEVER stored in the repository — only in
``infra/secrets/*`` on the VPS (docs/archive/INFRA.md, ADR-016).

On importing the module no network is touched and the token is not read eagerly — the settings
are instantiated lazily via :func:`get_bot_settings`, so that importing the package and
the unit tests do not require a valid environment.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class BotSettings(BaseSettings):
    """The Telegram bot settings.

    Attributes:
        tg_bot_token: the bot token from @BotFather (ENV ``TG_BOT_TOKEN``).
        bot_username: the bot’s username without ``@`` (ENV ``BOT_USERNAME``) — needed by
            the cabinet for the deep-link binding ``https://t.me/<bot_username>?start=
            <token>`` (cabinet↔bot stitching, C-04). Default ``Nabrs_bot``.
        tg_webhook_base_url: the public base URL for the webhook
            (for example ``https://nab.rs``). Empty → the webhook is not configured,
            polling is used (the owner’s local test).
        tg_webhook_path: the path on which aiohttp accepts the Telegram updates.
        tg_webhook_secret: the secret_token for checking the header
            ``X-Telegram-Bot-Api-Secret-Token`` (protection of the webhook endpoint).
        bot_signup_mode: the sign-up mode in the bot (ENV ``BOT_SIGNUP_MODE``).
            ``token_only`` (default) — ``/start`` without a valid deep-link
            token and without a bound channel is rejected, the subscription only
            via the site; ``open`` — the former free onboarding behavior.
    """

    model_config = SettingsConfigDict(
        env_prefix="",
        env_file=".env",
        extra="ignore",
    )

    tg_bot_token: str = Field(default="", alias="TG_BOT_TOKEN")
    bot_username: str = Field(default="Nabrs_bot", alias="BOT_USERNAME")
    tg_webhook_base_url: str = Field(default="", alias="TG_WEBHOOK_BASE_URL")
    tg_webhook_path: str = Field(default="/webhook/telegram", alias="TG_WEBHOOK_PATH")
    tg_webhook_secret: str = Field(default="", alias="TG_WEBHOOK_SECRET")

    # The chat ID of the administrator (owner) for decisions on the trial access (STAGE T).
    # 0 = not configured (the callback tr:/the /trials command admit nobody).
    admin_chat_id: int = Field(default=0, alias="ADMIN_CHAT_ID")

    bot_signup_mode: str = Field(default="token_only", alias="BOT_SIGNUP_MODE")

    @property
    def webhook_url(self) -> str:
        """The full webhook URL (base + path). Empty if the base is not set."""
        if not self.tg_webhook_base_url:
            return ""
        return self.tg_webhook_base_url.rstrip("/") + self.tg_webhook_path


@lru_cache
def get_bot_settings() -> BotSettings:
    """Lazily instantiates and caches the settings.

    Called in the factories of the bot/dispatcher, not at the module level — therefore the import of
    :mod:`src.bots.telegram` does not require ``TG_BOT_TOKEN`` to be set.
    """
    return BotSettings()
