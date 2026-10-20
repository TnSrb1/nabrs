"""Unit tests of the provider-neutral helpers of src/billing/util.py.

``header_get`` — the case-insensitive read (the Starlette Headers already are such, but a dict from the tests is not). ``test_checkout_url`` — the deterministic test URL
(BILLING_LIVE=false): the separator ?/& and the markers provider/plan/tenant.
The function with the ``test_`` prefix is called via the module, so that pytest does not collect it.
"""

from __future__ import annotations

from src.billing import util
from src.billing.util import header_get

# --------------------------------------------------------------------------- #
#  header_get
# --------------------------------------------------------------------------- #


def test_header_get_exact_key():
    assert header_get({"Stripe-Signature": "abc"}, "Stripe-Signature") == "abc"


def test_header_get_case_insensitive_fallback():
    # dict is case-sensitive — we get into the fallback branch via .lower().
    assert header_get({"stripe-signature": "xyz"}, "Stripe-Signature") == "xyz"
    assert header_get({"PADDLE-SIGNATURE": "p"}, "Paddle-Signature") == "p"


def test_header_get_missing_returns_none():
    assert header_get({"Content-Type": "application/json"}, "X-Absent") is None
    assert header_get({}, "Any") is None


# --------------------------------------------------------------------------- #
#  test_checkout_url (the test mode of the billing)
# --------------------------------------------------------------------------- #


def test_checkout_url_appends_query_with_question_mark():
    url = util.test_checkout_url("stripe", "pro", "tenant-1", "https://nab.rs/app")
    assert url.startswith("https://nab.rs/app?")
    assert "billing_test=1" in url
    assert "provider=stripe" in url
    assert "plan=pro" in url
    assert "tenant=tenant-1" in url


def test_checkout_url_uses_ampersand_when_query_present():
    # success_url already has '?': the markers are added via '&', not a second '?'.
    url = util.test_checkout_url("paddle", "business", "t9", "https://nab.rs/app?ref=x")
    assert "https://nab.rs/app?ref=x&billing_test=1" in url
    assert url.count("?") == 1
    assert "provider=paddle" in url
    assert "plan=business" in url
