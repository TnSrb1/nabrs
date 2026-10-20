"""Database layer for Tender Bot Serbia.

Exposes the declarative :class:`~src.db.base.Base`. ORM models live in
:mod:`src.db.models`.
"""

from __future__ import annotations

from src.db.base import Base

__all__ = ["Base"]
