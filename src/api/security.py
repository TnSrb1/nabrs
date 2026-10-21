"""The signature of the session cookie and the work with one-off tokens (Sprint-19).

No external dependencies: HMAC-SHA256 on top of the stdlib. This is a deliberate choice — do not pull in itsdangerous; the logic is trivial and fully testable.

Purpose:
- :func:`sign_session` / :func:`verify_session` — a passwordless session in a signed cookie (ARCHITECTURE §4). Payload = ``{"uid","tid","iat"}``; the HMAC signature, the max_age check by ``iat``.
- :func:`generate_token` — a cryptographically strong one-off token (magic link, email verify). The raw token goes into the URL, and only its SHA-256 is stored in the DB (:func:`hash_token`) — a compromise of the DB does not reveal the working links.
- :func:`hash_value` / :func:`normalize_email` — the normalization and the hash of the value of the anti-abuse identifier (DB_SCHEMA §2.19): only SHA-256 goes into the DB.
- :func:`safe_next_path` — validation of the return parameter after login (protection against an open redirect).

The signing secret is the ENV ``CABINET_SECRET_KEY`` (see :mod:`src.api.config`); there is no secret in the repo (ADR-016 / 028), the default is an obviously unsafe dev placeholder.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time

# A reasonable ceiling for ``next``: no internal cabinet path is longer than that,
# and it cuts obviously garbage values before any parsing.
_MAX_NEXT_LEN = 512


def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64d(data: str) -> bytes:
    pad = "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(data + pad)


def _sign(payload_b64: str, secret: str) -> str:
    digest = hmac.new(
        secret.encode("utf-8"), payload_b64.encode("ascii"), hashlib.sha256
    ).digest()
    return _b64e(digest)


def sign_session(
    uid: str,
    tid: str,
    secret: str,
    *,
    locale: str | None = None,
    now: int | None = None,
) -> str:
    """Signs the session (user_id + tenant_id + issued-at) → ``payload.sig``.

    ``locale`` — the interface language of the cabinet (``users.locale``): we put it into the signed session, so that the i18n chrome does not read it from the DB on EVERY render. The key is added ONLY if the value is set — the old contract (uid / tid / iat) for calls without a locale does not change (cookie backward compatibility).
    """
    body = {"uid": uid, "tid": tid, "iat": int(now if now is not None else time.time())}
    if locale is not None:
        body["locale"] = locale
    payload_b64 = _b64e(json.dumps(body, separators=(",", ":")).encode("utf-8"))
    return f"{payload_b64}.{_sign(payload_b64, secret)}"


def verify_session(
    token: str | None,
    secret: str,
    *,
    max_age: int = 60 * 60 * 24 * 14,
    now: int | None = None,
) -> dict | None:
    """Checks the cookie. Returns the payload (``uid`` / ``tid`` / ``iat``) or ``None``.

    ``None`` on: an empty token, a wrong format, a wrong signature (the comparison in constant time), an expired ``max_age``.
    """
    if not token or "." not in token:
        return None
    payload_b64, _, sig = token.partition(".")
    expected = _sign(payload_b64, secret)
    if not hmac.compare_digest(sig, expected):
        return None
    try:
        body = json.loads(_b64d(payload_b64))
    except (ValueError, json.JSONDecodeError):
        return None
    iat = body.get("iat")
    if not isinstance(iat, int):
        return None
    current = int(now if now is not None else time.time())
    if current - iat > max_age:
        return None
    if not body.get("uid") or not body.get("tid"):
        return None
    return body


def csrf_token(session: dict | None, secret: str) -> str:
    """The CSRF token of the cabinet’s POST forms — an HMAC of the session’s identity (``uid`` / ``iat``).

    No separate cookie and no state: the token is derived from the already signed session, so a foreign site that does not know ``CABINET_SECRET_KEY`` cannot forge it, and a new session (a new ``iat``) gives a new token. Anonymous → ``""`` (such a token is never accepted by ``verify_csrf``).
    """
    if not session or not session.get("uid") or not isinstance(session.get("iat"), int):
        return ""
    msg = f"csrf:{session['uid']}:{session['iat']}"
    return _sign(_b64e(msg.encode("utf-8")), secret)


def verify_csrf(token: str | None, session: dict | None, secret: str) -> bool:
    """Checks the form token against the expected one in constant time; an empty one — always a refusal."""
    expected = csrf_token(session, secret)
    if not expected or not token:
        return False
    return hmac.compare_digest(token, expected)


def safe_next_path(value: str | None) -> str | None:
    """Validates the return parameter after login. ``None`` — discard the value.

    Protection against an open redirect: we let through ONLY a relative internal path. We reject everything by which the browser could be led to a foreign host or which would forge the ``Location`` header:
    - does not start with a single ``/`` (including ``//evil.com``, ``https://…``);
    - contains ``\\`` (some browsers treat ``/\\evil.com`` as ``//``);
    - contains ``:`` (a scheme — ``javascript:``, ``http:``);
    - contains a line break / a control character (CRLF injection into Location).

    The caller decides itself what to replace a discarded value with (the default) — there is deliberately no default here: the cabinet and the admin have different ones.
    """
    if not value or not isinstance(value, str):
        return None
    if len(value) > _MAX_NEXT_LEN:
        return None
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in value):
        return None
    if "\\" in value or ":" in value:
        return None
    if not value.startswith("/") or value.startswith("//"):
        return None
    return value


def generate_token(nbytes: int = 32) -> str:
    """A cryptographically strong URL-safe token for one-off links."""
    return secrets.token_urlsafe(nbytes)


def hash_token(raw: str) -> str:
    """The SHA-256 hex of the one-off token (only the hash is stored in the DB)."""
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def normalize_email(email: str) -> str:
    """Normalises the e-mail for hashing / comparison: trim + lowercase."""
    return (email or "").strip().lower()


# The providers for which dots in the local part are insignificant, and the synonymous domains lead
# to one mailbox. The list is deliberately short: canonicalisation “just in case” for
# an arbitrary domain would glue DIFFERENT people into one anti-abuse identifier.
_DOTLESS_LOCAL_DOMAINS = {"gmail.com", "googlemail.com"}
_DOMAIN_ALIASES = {"googlemail.com": "gmail.com"}


def canonical_email_for_abuse(email: str) -> str:
    """The canonical form of the address ONLY for the anti-abuse identifier (§2.19).

    ``UNIQUE(value_hash) WHERE active`` gives the rule “one trial per address in 12 months”, but it was computed from :func:`normalize_email` (trim + lower), so the rule was bypassed by plus-addressing: ``user+1@…``, ``user+2@…`` — different hashes, as many Pro-rights self-service trials as you like.

    Canonicalisation: lower / trim → strip the ``+tag`` in the local part → for the gmail family remove the dots and reduce ``googlemail.com`` to ``gmail.com``.

    ‼️ Apply EXCLUSIVELY to ``value_hash``. ``users.email`` and the login key remain on :func:`normalize_email`: canonicalising them would mean changing the address the person logs in with and the one the email goes to.
    """
    norm = normalize_email(email)
    if "@" not in norm:
        return norm
    local, _, domain = norm.rpartition("@")
    domain = _DOMAIN_ALIASES.get(domain, domain)
    local = local.split("+", 1)[0]
    if domain in _DOTLESS_LOCAL_DOMAINS:
        local = local.replace(".", "")
    if (
        not local
    ):  # an address of the form “+tag@…” — nothing to canonicalise, we take it as is
        return norm
    return f"{local}@{domain}"


def hash_value(value: str) -> str:
    """The SHA-256 hex of the normalized value of the anti-abuse identifier (§2.19)."""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def email_last4(email: str) -> str:
    """The last 4 characters of the e-mail — for ``anti_abuse_identifiers.value_last4``."""
    norm = normalize_email(email)
    return norm[-4:] if norm else ""
