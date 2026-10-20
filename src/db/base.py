"""SQLAlchemy 2.0 declarative base for Tender Bot Serbia.

Single ``Base`` used by all ORM models in :mod:`src.db.models`. Its
``metadata`` is consumed by Alembic (``alembic/env.py`` ->
``target_metadata``) for autogenerate comparisons against the live DB.
"""

from __future__ import annotations

from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    """Declarative base for all models."""

    pass
