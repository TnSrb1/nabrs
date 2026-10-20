"""Tests of the fail-hard validator CABINET_SECRET_KEY (AUD-003 / FIX-04).

We use CabinetSettings(...) directly — without get_cabinet_settings() — so that
@lru_cache does not mask the result of instantiation.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from src.api.config import CabinetSettings

_PROD_DOMAIN = "app.nab.rs"
_DEV_DOMAIN = "localhost"
_DEFAULT_SECRET = "dev-insecure-change-me"
_VALID_SECRET = "s3cr3t-v4l1d-k3y-f0r-t3st"


def test_prod_default_secret_raises():
    """Prod domain + default secret → ValidationError on instantiation."""
    with pytest.raises(ValidationError, match="CABINET_SECRET_KEY"):
        CabinetSettings(DOMAIN=_PROD_DOMAIN, CABINET_SECRET_KEY=_DEFAULT_SECRET)


def test_prod_empty_secret_raises():
    """Prod domain + empty CABINET_SECRET_KEY → ValidationError."""
    with pytest.raises(ValidationError, match="CABINET_SECRET_KEY"):
        CabinetSettings(DOMAIN=_PROD_DOMAIN, CABINET_SECRET_KEY="")


def test_prod_valid_secret_ok():
    """Prod domain + non-trivial secret → no exception."""
    settings = CabinetSettings(DOMAIN=_PROD_DOMAIN, CABINET_SECRET_KEY=_VALID_SECRET)
    assert settings.domain == _PROD_DOMAIN


def test_dev_default_secret_ok():
    """localhost + default secret → OK (dev/CI do not fail)."""
    settings = CabinetSettings(DOMAIN=_DEV_DOMAIN, CABINET_SECRET_KEY=_DEFAULT_SECRET)
    assert settings.cabinet_secret_key == _DEFAULT_SECRET


def test_billing_defaults_test_mode():
    """Billing default (VG-11.1): stripe, test mode, empty secrets."""
    s = CabinetSettings(DOMAIN=_DEV_DOMAIN)
    assert s.billing_provider == "stripe"
    assert s.billing_live is False
    assert s.paddle_api_key == ""
    assert s.paddle_webhook_secret == ""
    assert s.stripe_api_key == ""
    assert s.stripe_webhook_secret == ""


def test_billing_env_override():
    """ENV overrides the billing provider/flag/secrets."""
    s = CabinetSettings(
        DOMAIN=_DEV_DOMAIN,
        BILLING_PROVIDER="paddle",
        BILLING_LIVE="true",
        PADDLE_WEBHOOK_SECRET="whs",
    )
    assert s.billing_provider == "paddle"
    assert s.billing_live is True
    assert s.paddle_webhook_secret == "whs"
