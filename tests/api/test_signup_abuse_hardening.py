"""Anti-abuse of the sign-up before public access: disposable mailboxes and aliases.

Without the network (ADR-013): Turnstile/Kickbox are stubs, no e-mail is sent.

Two holes that this closes (both verified on production 26.07.2026):
1. ``KICKBOX_API_KEY`` is empty → the validator gave ``disposable=False`` (fail-open), and `signup` blocks only ``disposable or undeliverable`` — so a temp-mail passed and got 14 days of Pro rights in self-service.
2. ``value_hash`` was computed from ``normalize_email`` (trim+lower), therefore the rule “one trial per address in 12 months” (UNIQUE(value_hash) WHERE active) was bypassed by plus-addressing: ``user+1@…``, ``user+2@…``.
"""

from __future__ import annotations

import pytest

from src.api import anti_abuse as aa
from src.api import security

# --------------------------------------------------------------------------- #
# Disposable domains (they work WITHOUT a Kickbox key)
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "email",
    [
        "grab@mailinator.com",
        "x@temp-mail.org",
        "y@guerrillamail.com",
        "z@yopmail.com",
        "Q@TrashMail.com",  # the case does not save
    ],
)
def test_disposable_domains_are_recognized(email):
    assert aa.is_disposable_domain(email) is True


@pytest.mark.parametrize(
    "email",
    [
        "buyer@gmail.com",
        "office@nabavka.rs",
        "ivan@company.co.rs",
        "no-at-sign",
        "",
    ],
)
def test_regular_addresses_are_not_disposable(email):
    """Filtering by the FULL domain: a substring heuristic would kill live domains."""
    assert aa.is_disposable_domain(email) is False


@pytest.mark.asyncio
async def test_validator_blocks_disposable_without_api_key():
    """‼️ The core: there is no key on production, and before this fix a temp-mail passed."""
    verdict = await aa.KickboxValidator("").validate("grab@mailinator.com")
    assert verdict.disposable is True
    assert verdict.reason == "disposable_domain"


@pytest.mark.asyncio
async def test_validator_stays_fail_open_for_normal_domain_without_api_key():
    """Regression: an ordinary address without a key is still NOT blocked."""
    verdict = await aa.KickboxValidator("").validate("buyer@gmail.com")
    assert verdict.disposable is False
    assert verdict.undeliverable is False
    assert verdict.reason == "no_api_key"


@pytest.mark.asyncio
async def test_disposable_check_precedes_network_call():
    """With a key the network for a disposable domain is not called at all.

    httpx is present in this environment, so “did not fail” would prove nothing: the verdict `disposable_domain` is possible ONLY from the local branch before the call.
    """
    verdict = await aa.KickboxValidator("live-key").validate("x@dropmail.me")
    assert verdict.reason == "disposable_domain"


# --------------------------------------------------------------------------- #
# The canonicalisation of the address for the anti-abuse identifier
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "alias",
    [
        "user+1@gmail.com",
        "user+whatever@gmail.com",
        "u.s.e.r@gmail.com",
        "USER@Gmail.com",
        "  user@googlemail.com  ",
    ],
)
def test_gmail_aliases_share_one_identifier(alias):
    """All the forms of one mailbox give ONE value_hash → the second trial is not granted."""
    canon = security.canonical_email_for_abuse("user@gmail.com")
    assert security.canonical_email_for_abuse(alias) == canon
    assert security.hash_value(security.canonical_email_for_abuse(alias)) == (
        security.hash_value(canon)
    )


def test_plus_tag_is_stripped_for_any_domain():
    assert security.canonical_email_for_abuse("ivan+tender@company.rs") == (
        "ivan@company.rs"
    )


def test_dots_are_significant_outside_gmail():
    """‼️ We glue dots ONLY in the gmail family.

    On corporate domains ``i.petrovic@`` and ``ipetrovic@`` are different people; a canonicalisation “just in case” would glue them into one identifier and deprive the second one of a trial for nothing.
    """
    assert security.canonical_email_for_abuse("i.petrovic@company.rs") != (
        security.canonical_email_for_abuse("ipetrovic@company.rs")
    )


def test_login_key_is_not_canonicalized():
    """`normalize_email` (the login key and users.email) stays untouched."""
    assert security.normalize_email("User+1@Gmail.com") == "user+1@gmail.com"


@pytest.mark.parametrize("weird", ["+tag@gmail.com", "no-at-sign", ""])
def test_degenerate_inputs_do_not_produce_empty_identifier(weird):
    """An empty local-part must not collapse DIFFERENT addresses into one hash."""
    assert security.canonical_email_for_abuse(weird) == security.normalize_email(weird)
