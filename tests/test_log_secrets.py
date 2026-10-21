"""Masking of the secrets in the httpx log (tmp/TASK_ingest_alert_debounce.md,
Part 2/2b — the leak of the token of the admin bot and the accessToken of the portal into `docker logs`).

The expectations are literal (not via ``_mask``/``SecretMaskingFilter`` of the code itself — otherwise `assert mask(x) == mask(x)` would be a tautology, ‼️ explicitly forbidden by the brief): we build a real ``logging.LogRecord`` the way httpx forms it (``record.msg`` — the format string, the URL — a positional ``httpx.URL`` in the args), and compare the resulting ``record.getMessage()`` with the EXPECTED string.

The tokens in the tests are only synthetic (``bot12345:AAF_TEST``, ``accessToken=SYNTH_TEST_VALUE``), not real ones.
"""

from __future__ import annotations

import logging

import httpx
import pytest

from src.log_secrets import SecretMaskingFilter, install_secret_log_filter


def _httpx_style_record(method: str, url: str) -> logging.LogRecord:
    """Builds a LogRecord exactly the way httpx._client does (see
    site-packages/httpx/_client.py: `logger.info('HTTP Request: %s %s "%s %d
    %s"', request.method, request.url, http_version, status_code, reason)`).
    """
    record = logging.LogRecord(
        name="httpx",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg='HTTP Request: %s %s "%s %d %s"',
        args=(method, httpx.URL(url), "HTTP/1.1", 200, "OK"),
        exc_info=None,
    )
    return record


# --------------------------------------------------------------------------- #
#  The red cycle (BEFORE the fix) — is documented in TESTING, not here: without the filter
#  httpx.URL is printed in full, and that is the original leak. Below —
#  the counter-facts WITH the filter.
# --------------------------------------------------------------------------- #
def test_telegram_bot_token_masked_in_path():
    record = _httpx_style_record(
        "POST", "https://api.telegram.org/bot12345:AAF_TEST/sendMessage"
    )
    SecretMaskingFilter().filter(record)
    text = record.getMessage()
    assert "AAF_TEST" not in text
    assert "12345" not in text
    assert (
        text == "HTTP Request: POST https://api.telegram.org/bot***/sendMessage "
        '"HTTP/1.1 200 OK"'
    )


def test_portal_user_token_masked_in_query():
    record = _httpx_style_record(
        "GET",
        "https://jnportal.src-ujn.example.invalid/GetDocument.ashx?id=42&accessToken=SYNTH_TEST_VALUE",
    )
    SecretMaskingFilter().filter(record)
    text = record.getMessage()
    assert "SYNTH_TEST_VALUE" not in text
    assert (
        text
        == "HTTP Request: GET https://jnportal.src-ujn.example.invalid/GetDocument.ashx"
        '?id=42&accessToken=*** "HTTP/1.1 200 OK"'
    )


def test_generic_token_named_param_masked_by_pattern():
    """Masking by the NAME of the parameter (`*Token=`), not by the list of values — the brief’s requirement 2b: tomorrow a third secret with another name will be added, but with the same suffix ``Token``/``token``."""
    record = _httpx_style_record(
        "GET", "https://example.test/api?access_token=SYNTH_OTHER_SECRET"
    )
    SecretMaskingFilter().filter(record)
    text = record.getMessage()
    assert "SYNTH_OTHER_SECRET" not in text
    assert "access_token=***" in text


def test_request_without_secret_passes_through_unchanged():
    """Counter-fact: an ordinary request to the portal (without a token in the URL) — the line does NOT change (the brief’s requirement: do not spoil the diagnostics on 429/the count of requests)."""
    record = _httpx_style_record(
        "GET",
        "https://jnportal.src-ujn.example.invalid/api/PublicProcurement/Search?page=1",
    )
    original = record.getMessage()
    SecretMaskingFilter().filter(record)
    assert record.getMessage() == original
    assert "page=1" in record.getMessage()


def test_filter_returns_true_never_drops_record():
    record = _httpx_style_record("POST", "https://api.telegram.org/bot1:X/sendMessage")
    assert SecretMaskingFilter().filter(record) is True


def test_install_secret_log_filter_attaches_to_httpx_logger():
    httpx_logger = logging.getLogger("httpx")
    before = list(httpx_logger.filters)
    try:
        install_secret_log_filter()
        assert any(isinstance(f, SecretMaskingFilter) for f in httpx_logger.filters)
    finally:
        httpx_logger.filters = before


@pytest.mark.parametrize(
    "bad_url",
    [
        "https://api.telegram.org/bot999999:AAG-Synth_Token99/sendMessage",
        "https://jnportal.src-ujn.example.invalid/x?a=1&AccessToken=Synth2",
    ],
)
def test_end_to_end_through_installed_filter(bad_url, caplog):
    """A simulation of the real path: the httpx logger with the filter installed — as in install_secret_log_filter() — and a real httpx.info() call."""
    httpx_logger = logging.getLogger("httpx")
    before = list(httpx_logger.filters)
    httpx_logger.addFilter(SecretMaskingFilter())
    try:
        with caplog.at_level(logging.INFO, logger="httpx"):
            httpx_logger.info(
                'HTTP Request: %s %s "%s %d %s"',
                "GET",
                httpx.URL(bad_url),
                "HTTP/1.1",
                200,
                "OK",
            )
        assert "Synth" not in caplog.text
        assert "999999" not in caplog.text
    finally:
        httpx_logger.filters = before
