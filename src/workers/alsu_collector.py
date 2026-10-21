"""Snapshot stub: the property-sales collector of a specific source is not published."""

from __future__ import annotations

from typing import Any


def alsu_enabled() -> bool:
    return False


async def collect_asset_sales(session_factory: Any) -> None:
    raise NotImplementedError(
        "asset-sales collector is not part of the public snapshot"
    )
