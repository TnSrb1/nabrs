"""Masking of secrets in the httpx log (a common module for all workers and the bot).

REASON (fact 12.09.2026, tmp/TASK_ingest_alert_debounce.md). The httpx logger at
INFO level prints the FULL request URL — and for the Telegram Bot API the token of the admin bot
`@tendersrb_bot` is part of the PATH (`/bot<token>/sendMessage`), while for the jnportal portal
`accessToken` is part of the QUERY STRING of the enrichment. Both end up as text in
`docker logs`, which anyone can read.

DECISION. Do NOT silence the httpx logger entirely (`setLevel(WARNING)`) — that would kill
the diagnostics of requests to the portal (request count/429, finding the cause of a source
outage, incident 12.09.2026). Instead — a `logging.Filter` that masks the
SECRET in the message text; the line itself (method, status, response code) stays.

Masking is by PATTERN (the path `/bot<token>`, the name of the query parameter `*Token=`),
not by enumerating concrete values: an enumeration of literals is not a predicate,
tomorrow a third secret with a different parameter name will appear.

Install `install_secret_log_filter()` right after `logging.basicConfig()` in
each entry point (a worker is a separate process/container, the logging configuration is not shared between them).
"""

from __future__ import annotations

import logging
import re

# Telegram Bot API: a path of the form /bot<digits>:<secret>/<method>.
_BOT_TOKEN_RE = re.compile(r"/bot\d+:[A-Za-z0-9_-]+")
# Any query parameter whose name ends with Token/token (accessToken
# of the portal, access_token, etc.) — we mask the value and keep the parameter name
# (the diagnostics of “which token has expired” matter more than the value itself).
_QUERY_TOKEN_RE = re.compile(r"([?&][A-Za-z0-9_]*[Tt]oken=)[^&\s\"]+")


def _mask(message: str) -> str:
    message = _BOT_TOKEN_RE.sub("/bot***", message)
    message = _QUERY_TOKEN_RE.sub(r"\1***", message)
    return message


class SecretMaskingFilter(logging.Filter):
    """Replaces the secret in the finished log message text with ``***``.

    Works through ``record.getMessage()`` (and not through ``record.args`` directly),
    because httpx passes the URL as an ``httpx.URL`` object in a positional
    argument — the already formatted string must be masked. Messages without a
    secret are returned UNCHANGED (``record`` is not touched at all).
    """

    def filter(self, record: logging.LogRecord) -> bool:
        original = record.getMessage()
        masked = _mask(original)
        if masked != original:
            record.msg = masked
            record.args = ()
        return True


def install_secret_log_filter() -> None:
    """Attaches the filter to the ``httpx`` logger. NOT idempotent: a repeated call
    would add a second filter — call it EXACTLY ONCE per process, right after
    ``logging.basicConfig()``."""
    logging.getLogger("httpx").addFilter(SecretMaskingFilter())
