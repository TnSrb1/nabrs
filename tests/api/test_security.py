"""Tests of the session cookie signature and the one-time tokens (:mod:`src.api.security`).

Pure stdlib functions, without DB/network. We cover the round-trip of the signature, all the refusal branches of :func:`verify_session` (format, signature, TTL, a corrupted payload, empty fields) and the determinism of hashes.

The ``security.py`` itself is not changed (red path) — only the tests.
"""

from __future__ import annotations

import base64
import json

import pytest

from src.api import security
from src.api.security import (
    email_last4,
    generate_token,
    hash_token,
    hash_value,
    normalize_email,
    sign_session,
    verify_session,
)

SECRET = "test-secret-key"


# --- sign_session / verify_session round-trip --------------------------------


def test_sign_session_format():
    token = sign_session("u1", "t1", SECRET, now=1000)
    assert token.count(".") == 1
    payload_b64, _, sig = token.partition(".")
    assert payload_b64 and sig


def test_sign_verify_round_trip():
    token = sign_session("user-42", "tenant-7", SECRET, now=1000)
    body = verify_session(token, SECRET, now=1000)
    assert body == {"uid": "user-42", "tid": "tenant-7", "iat": 1000}


def test_sign_session_uses_time_when_now_none(monkeypatch):
    monkeypatch.setattr(security.time, "time", lambda: 5555.9)
    token = sign_session("u", "t", SECRET)
    body = verify_session(token, SECRET, now=5555)
    assert body["iat"] == 5555


def test_verify_session_uses_time_when_now_none(monkeypatch):
    token = sign_session("u", "t", SECRET, now=1000)
    monkeypatch.setattr(security.time, "time", lambda: 1010.0)
    body = verify_session(token, SECRET, max_age=100)
    assert body is not None and body["uid"] == "u"


# --- verify_session: refusal branches --------------------------------------------


@pytest.mark.parametrize("bad", [None, "", "no-dot-here"])
def test_verify_session_empty_or_no_dot(bad):
    assert verify_session(bad, SECRET) is None


def test_verify_session_bad_signature():
    token = sign_session("u1", "t1", SECRET, now=1000)
    payload_b64, _, _ = token.partition(".")
    tampered = f"{payload_b64}.{security._b64e(b'wrong-sig')}"
    assert verify_session(tampered, SECRET, now=1000) is None


def test_verify_session_wrong_secret():
    token = sign_session("u1", "t1", SECRET, now=1000)
    assert verify_session(token, "other-secret", now=1000) is None


def test_verify_session_expired():
    token = sign_session("u1", "t1", SECRET, now=1000)
    # now is 1 second over max_age
    assert verify_session(token, SECRET, max_age=10, now=1011) is None


def test_verify_session_at_max_age_boundary():
    token = sign_session("u1", "t1", SECRET, now=1000)
    # exactly on the boundary (current - iat == max_age) — still valid
    assert verify_session(token, SECRET, max_age=10, now=1010) is not None


def test_verify_session_corrupted_payload():
    # a valid signature over a non-base64/non-JSON payload
    bad_payload = "!!!not-base64!!!"
    token = f"{bad_payload}.{security._sign(bad_payload, SECRET)}"
    assert verify_session(token, SECRET, now=1000) is None


def test_verify_session_iat_not_int():
    body = {"uid": "u", "tid": "t", "iat": "nope"}
    payload_b64 = security._b64e(json.dumps(body).encode("utf-8"))
    token = f"{payload_b64}.{security._sign(payload_b64, SECRET)}"
    assert verify_session(token, SECRET, now=1000) is None


def test_verify_session_missing_iat():
    body = {"uid": "u", "tid": "t"}
    payload_b64 = security._b64e(json.dumps(body).encode("utf-8"))
    token = f"{payload_b64}.{security._sign(payload_b64, SECRET)}"
    assert verify_session(token, SECRET, now=1000) is None


@pytest.mark.parametrize(
    "body",
    [
        {"uid": "", "tid": "t", "iat": 1000},
        {"uid": "u", "tid": "", "iat": 1000},
        {"tid": "t", "iat": 1000},
        {"uid": "u", "iat": 1000},
    ],
)
def test_verify_session_empty_uid_or_tid(body):
    payload_b64 = security._b64e(json.dumps(body).encode("utf-8"))
    token = f"{payload_b64}.{security._sign(payload_b64, SECRET)}"
    assert verify_session(token, SECRET, now=1000) is None


# --- base64 helpers ----------------------------------------------------------


def test_b64_round_trip_strips_and_restores_padding():
    for raw in (b"", b"a", b"ab", b"abc", b"abcd", b"\x00\xff\x10"):
        assert security._b64d(security._b64e(raw)) == raw


def test_b64e_has_no_padding():
    assert "=" not in security._b64e(b"abc")


# --- generate_token / hash_token ---------------------------------------------


def test_generate_token_url_safe_and_unique():
    a = generate_token()
    b = generate_token()
    assert a != b
    # urlsafe alphabet: without '+', '/', '='
    assert all(c not in a for c in "+/=")


def test_generate_token_respects_nbytes():
    short = generate_token(8)
    long = generate_token(64)
    assert len(long) > len(short)


def test_hash_token_is_sha256_hex():
    digest = hash_token("magic-link-raw")
    assert len(digest) == 64
    assert int(digest, 16) >= 0  # a valid hex
    # determinism
    assert hash_token("magic-link-raw") == digest
    assert hash_token("other") != digest


# --- normalize_email / hash_value / email_last4 ------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("  User@Example.COM ", "user@example.com"),
        ("ALREADY@low.com", "already@low.com"),
        ("", ""),
        (None, ""),
    ],
)
def test_normalize_email(raw, expected):
    assert normalize_email(raw) == expected


def test_hash_value_deterministic_sha256():
    digest = hash_value("identifier")
    assert len(digest) == 64
    assert hash_value("identifier") == digest
    assert hash_value("identifier2") != digest


def test_hash_value_matches_hashlib():
    import hashlib

    assert hash_value("x") == hashlib.sha256(b"x").hexdigest()


@pytest.mark.parametrize(
    "email,expected",
    [
        ("user@example.com", ".com"),
        ("  Foo@Bar.IO ", "r.io"),
        ("ab", "ab"),
        ("", ""),
        (None, ""),
    ],
)
def test_email_last4(email, expected):
    assert email_last4(email) == expected


def test_b64d_accepts_unpadded_standard_input():
    # _b64e gives urlsafe-without-padding; we make sure _b64d completes it
    raw = b"\xfb\xff\xbe"  # the characters that give '-' and '_' in the urlsafe alphabet
    encoded = security._b64e(raw)
    assert security._b64d(encoded) == raw
    # the same string through the standard base64 gives another alphabet
    std = base64.urlsafe_b64encode(raw).rstrip(b"=").decode()
    assert security._b64d(std) == raw
