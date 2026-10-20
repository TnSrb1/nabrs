"""Snapshot stub: the procurement-plan collector of a specific source is not published."""

from __future__ import annotations

from typing import Any


def plans_enabled() -> bool:
    return False


async def collect_plans(adapter: Any, session_factory: Any) -> None:
    raise NotImplementedError("plans collector is not part of the public snapshot")
