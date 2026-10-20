"""Demo adapter ``example`` — the reference of the :class:`SourceAdapter` contract.

Reads ``sample.json`` next to the module: no network, no tokens. Shows the three
mandatory methods (``fetch_batch`` / ``build_source_uid`` / ``normalize``) and the
:class:`SourceCapabilities` declaration. Real adapters are not part of the public repo.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.parsers.base import (
    NormalizedTender,
    RawTender,
    SourceAdapter,
    register_source,
)
from src.parsers.capabilities import SourceCapabilities

SAMPLE = Path(__file__).with_name("sample.json")


def _dt(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


@register_source
class ExampleAdapter(SourceAdapter):
    source_key = "example"
    source_name = "Example (demo, local file)"
    base_url = "file://sample.json"
    country_codes = ["XX"]
    request_rate = 1.0
    default_polling_interval = 3600
    supported_languages = ["en"]
    capabilities = SourceCapabilities(
        deadline="if_stated",
        amount="if_stated",
        notice_description=False,
        documents_text=False,
        cpv=True,
        original_link=False,
        awards=False,
        card_language="en_original",
        public_url="https://example.invalid",
    )

    async def fetch_batch(self, since: datetime | None) -> AsyncIterator[RawTender]:
        rows: list[dict[str, Any]] = json.loads(SAMPLE.read_text(encoding="utf-8"))
        now = datetime.now(tz=timezone.utc)
        for row in rows:
            published = _dt(row["published"])
            if since is not None and published is not None and published <= since:
                continue
            yield RawTender(self.source_key, row, "XX", now)

    def build_source_uid(self, raw: RawTender) -> str:
        return str(raw.payload["id"])

    def normalize(self, raw: RawTender) -> NormalizedTender:
        p = raw.payload
        return NormalizedTender(
            source_key=self.source_key,
            source_uid=self.build_source_uid(raw),
            country_code=raw.country_code,
            title=p["title"],
            customer_name=p["buyer"],
            amount_original=p["amount"],
            currency_original=p["currency"],
            amount_eur=p["amount"] if p["currency"] == "EUR" else None,
            is_amount_estimated=True if p["amount"] is not None else None,
            published_at=_dt(p["published"]),
            deadline_at=_dt(p["deadline"]),
            classifications=[{"scheme": "CPV", "code": p["cpv"]}] if p["cpv"] else [],
            language="en",
        )
