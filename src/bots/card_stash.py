"""The stash of the collapsed tender card (“✖ Hide” → “↩ Restore”).

A collapsed message keeps one line, and the full text and the keyboard of the card
disappear irrecoverably after ``edit_text`` in Telegram. So that “↩ Restore” is honest,
the original is temporarily put into Redis (the same ``REDIS_URL`` as the bot and
the workers), key — ``card:{chat_id}:{message_id}``, TTL 7 days.

Redis is not a condition of work: if unavailable → :func:`stash_card` returns ``False``, and
the caller does NOT collapse the card (otherwise the hidden card could not be restored).
Into the log — only the class of the error.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

logger = logging.getLogger(__name__)

STASH_TTL_SEC = 7 * 24 * 3600
# A hanging Redis must not hang the button handler: a timeout → treated as “Redis unavailable”.
REDIS_TIMEOUT_SEC = 2

_client: Any = None


def _redis() -> Any:
    """The lazy async Redis client (one per process)."""
    global _client
    if _client is None:
        import redis.asyncio as aioredis

        _client = aioredis.from_url(
            os.getenv("REDIS_URL", "redis://localhost:6379/0"),
            decode_responses=True,
            socket_timeout=REDIS_TIMEOUT_SEC,
            socket_connect_timeout=REDIS_TIMEOUT_SEC,
        )
    return _client


def _key(chat_id: int, message_id: int) -> str:
    return f"card:{chat_id}:{message_id}"


async def stash_card(chat_id: int, message_id: int, payload: dict) -> bool:
    """Saves the original card. ``False`` — the save failed."""
    try:
        await _redis().set(
            _key(chat_id, message_id),
            json.dumps(payload, ensure_ascii=False),
            ex=STASH_TTL_SEC,
        )
        return True
    except (
        Exception
    ) as exc:  # noqa: BLE001 — Redis is not a condition of the bot’s work
        logger.warning("card stash failed: %s", type(exc).__name__)
        return False


async def load_card(chat_id: int, message_id: int) -> dict | None:
    """Fetches the original card; no record/Redis unavailable/garbage → ``None``."""
    try:
        raw = await _redis().get(_key(chat_id, message_id))
        if not raw:
            return None
        data = json.loads(raw)
        return data if isinstance(data, dict) else None
    except Exception as exc:  # noqa: BLE001
        logger.warning("card stash load failed: %s", type(exc).__name__)
        return None


async def drop_card(chat_id: int, message_id: int) -> None:
    """Deletes the stash after a successful restore (best-effort)."""
    try:
        await _redis().delete(_key(chat_id, message_id))
    except Exception as exc:  # noqa: BLE001
        logger.warning("card stash drop failed: %s", type(exc).__name__)
