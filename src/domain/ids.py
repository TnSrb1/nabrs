"""The canonical form of identifiers from URLs.

Invariant: in the DB a uuid goes ONLY as the canonical string (36 characters, lower
case, hyphens). ``uuid.UUID`` also understands ``urn:uuid:…``, ``{…}`` and bare
32 characters, while asyncpg does not encode such strings → ``DataError`` → 500 (production, 08.10).
Therefore we bring the address to the canon rather than pass it through as is.
"""

from __future__ import annotations

import uuid


def canonical_uuid(value: str) -> str | None:
    """``str(UUID)`` for any form understood by ``uuid.UUID``; otherwise ``None``."""
    try:
        return str(uuid.UUID(value))
    except ValueError:
        return None
