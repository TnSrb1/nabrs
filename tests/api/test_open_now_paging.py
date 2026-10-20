"""The “Already open” block: the SQL pages are fetched further only if stage B filtered out the rows.

The fake session returns pre-set pages; we check the LIMIT/OFFSET parameters and the result set. The SQL predicate is not executed here — it is proven by
tests/db/test_open_now_examples.py.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from src.api.cabinet import preview_service


class _Result:
    def __init__(self, items):
        self._items = items

    def fetchall(self):
        return list(self._items)


class _Session:
    def __init__(self, pages):
        self._pages = list(pages)
        self.calls: list[dict] = []

    async def execute(self, stmt, params):
        self.calls.append({"lim": params["lim"], "off": params["off"]})
        return _Result(self._pages.pop(0))


def _row(i: int, title: str):
    return SimpleNamespace(
        id=f"t-{i}",
        title=title,
        title_normalized=title.lower(),
        description_normalized="",
        customer_name="Škola",
        deadline_at=None,
        customer_followed=False,
        archived=False,
    )


_PAYLOAD = {
    "enabled_source_keys": ["jnportal"],
    "match_all": False,
    "keywords_include": [{"all_of": ["racunarske"]}],
    "keywords_exclude": [],
    "classifications": {},
    "regions": [],
}


def test_open_now_pages_on_when_keyword_filter_rejects_first_page(monkeypatch):
    """The first page is entirely out of stage B → we take the next one (OFFSET = page)."""
    monkeypatch.setattr(preview_service, "OPEN_NOW_PAGE", 2)
    page1 = [_row(1, "Nabavka kancelarija"), _row(2, "Nabavka stolica")]
    page2 = [_row(3, "Nabavka racunarske opreme"), _row(4, "Racunarske usluge")]
    db = _Session([page1, page2])

    got = asyncio.run(
        preview_service.open_now_examples(db, _PAYLOAD, "profile-1", limit=2)
    )

    assert [ex.id for ex in got] == ["t-3", "t-4"]
    assert db.calls == [{"lim": 2, "off": 0}, {"lim": 2, "off": 2}]


def test_open_now_stops_after_first_page_when_limit_reached(monkeypatch):
    """The page reached the limit → we do not ask for a second SQL."""
    monkeypatch.setattr(preview_service, "OPEN_NOW_PAGE", 2)
    page1 = [_row(1, "Racunarske A"), _row(2, "Racunarske B")]
    db = _Session([page1])

    got = asyncio.run(
        preview_service.open_now_examples(db, _PAYLOAD, "profile-1", limit=2)
    )

    assert [ex.id for ex in got] == ["t-1", "t-2"]
    assert db.calls == [{"lim": 2, "off": 0}]
