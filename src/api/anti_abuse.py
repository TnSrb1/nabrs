"""Registration anti-abuse layer (ADR-018 revised, ARCHITECTURE §4).

Stack: Cloudflare Turnstile (server-side verify) + Kickbox email validation +
risk scoring. External calls — behind interfaces with live and mock/noop implementations
(ADR-013: tests do not call live APIs). httpx is imported LAZILY inside the
live methods — the module loads on stdlib and is tested without a network.

Keys (``TURNSTILE_SECRET_KEY``, ``KICKBOX_API_KEY``) — from ENV (see
:mod:`src.api.config`); the repo holds only placeholders (ADR-028).

The step-up €1 verification charge (Paddle SetupIntent) is NOT
implemented in this sprint — :func:`perform_setup_intent_step_up` raises
``NotImplementedError`` (Sprint-21, a future ADR). medium/high-risk registrations
are marked ``verification_required`` and do not get a cardless trial.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import NoReturn

TURNSTILE_VERIFY_URL = "https://challenges.cloudflare.com/turnstile/v0/siteverify"
KICKBOX_VERIFY_URL = "https://api.kickbox.com/v2/verify"

# Risk levels (risk scoring §4).
RISK_LOW = "low"
RISK_MEDIUM = "medium"
RISK_HIGH = "high"

# Numeric heuristic for anti_abuse_identifiers.risk_score_at_signup (0–100).
RISK_SCORE = {RISK_LOW: 10, RISK_MEDIUM: 60, RISK_HIGH: 90}


@dataclass(frozen=True)
class EmailVerdict:
    """Result of the email validation (Kickbox).

    Attributes:
        deliverable: the mailbox accepts mail (result=="deliverable").
        disposable: a one-time domain (mailinator/temp-mail/…) → block.
        risky: suspicious (role/accept_all/unknown) → medium risk.
        reason: machine code of the result for the log.
        undeliverable: Kickbox explicitly returned result=="undeliverable" → block.
            unknown/accept_all/risky/error — NOT undeliverable (fail-open).
    """

    deliverable: bool
    disposable: bool
    risky: bool
    reason: str = ""
    undeliverable: bool = False


# --------------------------------------------------------------------------- #
# Turnstile
# --------------------------------------------------------------------------- #


class TurnstileVerifier(ABC):
    """Server-side verification of the Turnstile token (siteverify)."""

    @abstractmethod
    async def verify(self, token: str | None, *, remoteip: str | None = None) -> bool:
        raise NotImplementedError


class LiveTurnstileVerifier(TurnstileVerifier):
    """Real check through the Cloudflare siteverify."""

    def __init__(self, secret_key: str, *, timeout: float = 10.0) -> None:
        self._secret = secret_key
        self._timeout = timeout

    async def verify(self, token: str | None, *, remoteip: str | None = None) -> bool:
        if not token or not self._secret:
            return False
        import httpx  # lazy: the module loads without network/dependency in tests

        data = {"secret": self._secret, "response": token}
        if remoteip:
            data["remoteip"] = remoteip
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            resp = await client.post(TURNSTILE_VERIFY_URL, data=data)
            resp.raise_for_status()
            return bool(resp.json().get("success"))


class NoopTurnstileVerifier(TurnstileVerifier):
    """Stub for dev/tests: returns the given result."""

    def __init__(self, result: bool = True) -> None:
        self._result = result

    async def verify(self, token: str | None, *, remoteip: str | None = None) -> bool:
        return self._result


# --------------------------------------------------------------------------- #
# Email validation
# --------------------------------------------------------------------------- #


# One-time mail domains — a local filter, NOT dependent on Kickbox.
# ‼️ Why: `KICKBOX_API_KEY` is empty on prod, and without the key the validator returned
# `disposable=False` (fail-open) — that is, temp-mail passed registration and
# got the trial period (TRIAL_DAYS) of Pro rights on self-service. The list closes the cheapest
# way to stamp out trials; it does not promise completeness — it is a filter, not the truth.
# With a key set, the list works BEFORE the network call (it saves a request), and
# the Kickbox verdict remains the source of truth for everything else.
_DISPOSABLE_DOMAINS = frozenset("""
    0-mail.com 10minutemail.com 10minutemail.net 20minutemail.com 33mail.com
    a-bc.net anonbox.net anonymbox.com armyspy.com bccto.me burnermail.io
    byom.de cool.fr.nf correo.blogos.net cust.in dayrep.com dcemail.com
    deadaddress.com despam.it dingbone.com discard.email discardmail.com
    disposeamail.com dispostable.com dodgeit.com dodgit.com dontreg.com
    dropmail.me e4ward.com email-fake.com emailfake.com emailondeck.com
    emailsensei.com emailtemporanea.net emailtemporario.com.br emltmp.com
    fakeinbox.com fakemail.net fakemailgenerator.com filzmail.com
    fleckens.hu gethmail.com getairmail.com getnada.com grr.la
    guerrillamail.biz guerrillamail.com guerrillamail.de guerrillamail.info
    guerrillamail.net guerrillamail.org guerrillamailblock.com harakirimail.com
    incognitomail.com inboxalias.com inboxbear.com jetable.org jourrapide.com
    kasmail.com koszmail.pl mail-temporaire.fr mail7.io mailcatch.com
    maildrop.cc mailexpire.com mailforspam.com mailinator.com mailinator.net
    mailmetrash.com mailnesia.com mailnull.com mailsac.com mailtemp.info
    mailtothis.com mailzilla.com meltmail.com mintemail.com moakt.com
    mohmal.com mt2015.com mytemp.email mytrashmail.com nada.email
    no-spam.ws nowmymail.com nwytg.net objectmail.com odaymail.com
    onetimemail.org owlymail.com pokemail.net proxymail.eu rcpt.at
    rhyta.com safetymail.info sharklasers.com shitmail.me
    slopsbox.com smashmail.de sogetthis.com spam4.me spamavert.com
    spambog.com spambox.us spamfree24.org spamgourmet.com spamhereplease.com
    spamspot.com superrito.com teleworm.us temp-mail.io temp-mail.org
    tempail.com tempemail.net tempinbox.com tempmail.dev tempmail.net
    tempmail.plus tempmailaddress.com tempmailo.com tempr.email
    thankyou2010.com throwawaymail.com tmail.ws tmailinator.com trash-mail.at
    trash-mail.com trashmail.com trashmail.de trashmail.me trashmail.net
    trbvm.com trbvn.com vpn.st wegwerfmail.de wegwerfmail.net
    wegwerfmail.org yopmail.com yopmail.fr yopmail.net yourdomain.com
    zetmail.com
    """.split())


def is_disposable_domain(email: str) -> bool:
    """The address domain — from the known one-time services.

    Comparison by the full domain (no “substring heuristics”): a substring rule
    like “contains temp” would cut off live corporate domains.
    """
    domain = (email or "").strip().lower().rpartition("@")[2]
    return bool(domain) and domain in _DISPOSABLE_DOMAINS


class EmailValidator(ABC):
    """Email validation at the registration stage."""

    @abstractmethod
    async def validate(self, email: str) -> EmailVerdict:
        raise NotImplementedError


class KickboxValidator(EmailValidator):
    """Real check through Kickbox real-time verification."""

    def __init__(self, api_key: str, *, timeout: float = 10.0) -> None:
        self._api_key = api_key
        self._timeout = timeout

    async def validate(self, email: str) -> EmailVerdict:
        # A local filter BEFORE everything else: it does not depend on the key or the network,
        # so it works the same without Kickbox (key empty on prod) and with it.
        if is_disposable_domain(email):
            return EmailVerdict(False, True, True, reason="disposable_domain")
        if not self._api_key:
            # fail-open: no key → risky, but NOT undeliverable (we do not block live ones)
            return EmailVerdict(False, False, True, reason="no_api_key")
        import httpx  # lazy

        try:
            params = {"email": email, "apikey": self._api_key}
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                resp = await client.get(KICKBOX_VERIFY_URL, params=params)
                resp.raise_for_status()
                data = resp.json()
        except Exception:
            # timeout/network/5xx → fail-open: risky, but NOT undeliverable
            return EmailVerdict(False, False, True, reason="kickbox_error")

        result = str(
            data.get("result", "")
        ).lower()  # deliverable/undeliverable/risky/unknown
        disposable = bool(data.get("disposable"))
        accept_all = bool(data.get("accept_all"))
        role = bool(data.get("role"))
        return EmailVerdict(
            deliverable=(result == "deliverable"),
            disposable=disposable,
            risky=(result in ("risky", "unknown") or accept_all or role),
            reason=result or "unknown",
            undeliverable=(result == "undeliverable"),
        )


class NoopEmailValidator(EmailValidator):
    """Stub for dev/tests: a verdict set in advance."""

    def __init__(
        self,
        *,
        deliverable: bool = True,
        disposable: bool = False,
        risky: bool = False,
        reason: str = "mock",
        undeliverable: bool = False,
    ) -> None:
        self._verdict = EmailVerdict(
            deliverable, disposable, risky, reason, undeliverable
        )

    async def validate(self, email: str) -> EmailVerdict:
        return self._verdict


# --------------------------------------------------------------------------- #
# Risk scoring + step-up
# --------------------------------------------------------------------------- #


def score_risk(turnstile_ok: bool, verdict: EmailVerdict) -> str:
    """Computes the registration risk level (ADR-018 §4).

    High — a Turnstile failure, a disposable domain, or an explicitly undeliverable one.
    unknown/accept_all/risky/kickbox_error → medium (not a block).
    """
    if not turnstile_ok:
        return RISK_HIGH
    if verdict.disposable or verdict.undeliverable:
        return RISK_HIGH
    if verdict.risky:
        return RISK_MEDIUM
    return RISK_LOW


def perform_setup_intent_step_up(*args, **kwargs) -> NoReturn:
    """Step-up €1 verification through Paddle SetupIntent — NOT implemented.

    medium/high-risk registrations require a card binding with an immediate
    refund (ADR-018 §4). Implementation — Sprint-21 (Paddle), after a future ADR.
    """
    raise NotImplementedError("step-up €1 SetupIntent — Sprint-21 (Paddle, a future ADR)")
