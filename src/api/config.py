"""The cabinet web configuration (pydantic-settings, Sprint-19).

Values come from ENV; secrets are NOT stored in the repository (ADR-016/028) — only obviously unsafe dev placeholders by default. The real keys of Turnstile / Kickbox / Postmark / SMTP and ``CABINET_SECRET_KEY`` live in ``infra/secrets/*`` on the VPS.

Instantiated lazily (:func:`get_cabinet_settings`), so that importing the package and the unit tests do not need a valid environment.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class CabinetSettings(BaseSettings):
    """Cabinet and anti-abuse settings."""

    model_config = SettingsConfigDict(env_prefix="", env_file=".env", extra="ignore")

    # The publication domain (Caddy / DOMAIN). It affects the Secure flag of the session cookie:
    # on localhost (dev) we do NOT set Secure, otherwise the browser would drop the cookie over http.
    domain: str = Field(default="localhost", alias="DOMAIN")
    # The base URL of the cabinet (for magic-link / email-verify links). ADR-026.
    # On production app.nab.rs, CABINET_BASE_URL=https://app.nab.rs MUST be set,
    # otherwise the links in the emails will lead to the apex landing page (there is no cabinet there).
    cabinet_base_url: str = Field(default="https://nab.rs", alias="CABINET_BASE_URL")
    # The signing secret of the session cookie. A PLACEHOLDER — replace it in production.
    cabinet_secret_key: str = Field(
        default="dev-insecure-change-me", alias="CABINET_SECRET_KEY"
    )
    cookie_secure: bool = Field(default=True, alias="CABINET_COOKIE_SECURE")
    session_max_age: int = Field(
        default=60 * 60 * 24 * 14, alias="CABINET_SESSION_MAX_AGE"
    )

    # Turnstile (ADR-018). secret — server-side, site — public (the widget).
    turnstile_secret_key: str = Field(default="", alias="TURNSTILE_SECRET_KEY")
    turnstile_site_key: str = Field(default="", alias="TURNSTILE_SITE_KEY")

    # Kickbox email validation.
    kickbox_api_key: str = Field(default="", alias="KICKBOX_API_KEY")

    # Postmark (outgoing transactional mail; the real integration — Sprint-6).
    postmark_server_token: str = Field(default="", alias="POSTMARK_SERVER_TOKEN")
    email_from: str = Field(default="noreply@nab.rs", alias="EMAIL_FROM")
    # The SMTP transport of outgoing mail (STAGE 2). If SMTP_HOST is set, it takes
    # priority over Postmark (see src.api.emailer.make_email_sender). SMTP_FROM
    # empty → a fallback to EMAIL_FROM.
    smtp_host: str = Field(default="", alias="SMTP_HOST")
    smtp_port: int = Field(default=587, alias="SMTP_PORT")
    smtp_user: str = Field(default="", alias="SMTP_USER")
    smtp_password: str = Field(default="", alias="SMTP_PASSWORD")
    smtp_from: str = Field(default="", alias="SMTP_FROM")
    # Telegram Bot API — the server-side admin notification about requests for trial
    # access (STAGE T). admin_chat_id=0 — not configured (no notification is
    # sent; the fallback is the /trials command in the bot).
    tg_bot_token: str = Field(default="", alias="TG_BOT_TOKEN")
    admin_chat_id: int = Field(default=0, alias="ADMIN_CHAT_ID")

    # TTL of links (seconds).
    email_verify_ttl: int = Field(default=60 * 60 * 24, alias="EMAIL_VERIFY_TTL")  # 24h
    magic_link_ttl: int = Field(default=60 * 15, alias="MAGIC_LINK_TTL")  # 15 min
    # TTL of the bind token of the Telegram channel binding (Chat 26, T3). Single-use, ~15 min.
    channel_bind_ttl: int = Field(default=60 * 15, alias="CHANNEL_BIND_TTL")  # 15 min
    trial_days: int = Field(default=30, alias="TRIAL_DAYS")
    # Enable the step-up verification (€1) for medium-risk (unknown / accept_all / risky).
    # Default false: Gmail and the large providers get a cardless trial.
    # Enable later: REQUIRE_STEP_UP_FOR_MEDIUM=true in secrets.env.
    require_step_up_for_medium: bool = Field(
        default=False, alias="REQUIRE_STEP_UP_FOR_MEDIUM"
    )

    # ── Billing (VG-11.1) ────────────────────────────────────────────────────
    # The active payment provider (the registry src/billing/registry.py): paddle|stripe.
    billing_provider: str = Field(default="stripe", alias="BILLING_PROVIDER")
    # Test mode by default: there is no real charge, create_checkout gives
    # a test URL “as if the payment works”. Live will be switched on by the owner with one flag (VG-11b).
    billing_live: bool = Field(default=False, alias="BILLING_LIVE")
    # The providers' keys / secrets — from env (do NOT commit them to the repo; the default is empty).
    # For the sandbox the owner sets the test keys in infra/secrets on the VPS.
    paddle_api_key: str = Field(default="", alias="PADDLE_API_KEY")
    paddle_webhook_secret: str = Field(default="", alias="PADDLE_WEBHOOK_SECRET")
    stripe_api_key: str = Field(default="", alias="STRIPE_API_KEY")
    stripe_webhook_secret: str = Field(default="", alias="STRIPE_WEBHOOK_SECRET")
    # The Stripe Price API price_id for each paid plan of the showcase (live checkout).
    # The field name = stripe_price_{plan_key} (see registry._stripe_prices, which reads by
    # name). Empty → the plan’s live checkout is fail-closed, the showcase says “Coming soon”. The values
    # are entered by the owner from the Stripe Dashboard (the details, do NOT commit to the repo).
    stripe_price_pro: str = Field(default="", alias="STRIPE_PRICE_PRO")
    stripe_price_business: str = Field(default="", alias="STRIPE_PRICE_BUSINESS")

    # The allowlist of emails of platform operators (access to /admin/*). CSV.
    # Empty → the admin console is closed to everyone (fail-closed). Set in secrets.env:
    # CABINET_ADMIN_EMAILS=op1@example.com,op2@example.com
    platform_admin_emails: str = Field(default="", alias="CABINET_ADMIN_EMAILS")

    @model_validator(mode="after")
    def _check_prod_secret(self) -> "CabinetSettings":
        """A hard fail in production on a default or empty CABINET_SECRET_KEY (AUD-003)."""
        is_prod = self.domain.strip().lower() not in {"localhost", "127.0.0.1", ""}
        insecure = self.cabinet_secret_key in {"", "dev-insecure-change-me"}
        if is_prod and insecure:
            raise ValueError(
                "CABINET_SECRET_KEY is not set or equals the unsafe default. "
                "Set a real secret in infra/secrets on the VPS (CABINET_SECRET_KEY)."
            )
        return self


@lru_cache
def get_cabinet_settings() -> CabinetSettings:
    """Lazily instantiates and caches the cabinet settings."""
    return CabinetSettings()
