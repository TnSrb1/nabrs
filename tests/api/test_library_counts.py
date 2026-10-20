"""The live number on the showcase card — THE SAME as the form preview shows.

The owner’s requirement: the client must see that the template is live BEFORE they click anything. The price of trust in the number is the uniqueness of the predicate: if the showcase and the preview diverge by even one tender, the number stops being read.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from src.api import deps
from src.api.cabinet import (
    library_counts,
    preview_service,
    routes_library,
    template_prefill,
    vertical_packages,
)
from src.api.main import app
from src.workers.matching import PREVIEW_WHERE_A
from tests.api.conftest import FakeResult, FakeSession

SESS = {"uid": "u-1", "tid": "t-1"}


# The collapsed tender notice texts — as the ingest writes them (Serbian Latin).
CORPUS = [
    ("nabavka racunarske opreme", "monitori i stampaci za skolu"),
    ("nabavka softvera i licenci", "godisnje licence za kancelarijski paket"),
    ("radovi na rekonstrukciji skole", "gradjevinski radovi, krov i fasada"),
    ("usluge ciscenja poslovnog prostora", "svakodnevno ciscenje"),
]


# The sources — as the showcase route gives them (`default_source_keys_for_new_profile`,
# production fact 30.09: jnportal enabled). Without a source the preview = 0 (SRC-0b:
# an empty selection no longer means “all sources”).
SRC = ["jnportal"]


def _rows(idx=None):
    return [
        SimpleNamespace(title_normalized=t, description_normalized=d, idx=idx)
        for t, d in CORPUS
    ]


def _tpl(name, terms, code):
    return SimpleNamespace(
        id=f"id-{name}",
        name=name,
        description=None,
        activity_kind="Тест",
        keywords_include=[{"all_of": [t]} for t in terms],
        classifications={
            "include": [{"system_code": "eu_cpv", "code": code}],
            "exclude": [],
        },
        i18n=None,
    )


TEMPLATES = [
    _tpl("Компьютеры", ["компьютер"], "30000000"),
    _tpl("Программное обеспечение", ["программное", "лицензия"], "48000000"),
    _tpl("Уборка", ["уборка"], "90910000"),
]


# --------------------------------------------------------------------------- #
# One predicate for both places
# --------------------------------------------------------------------------- #


def test_batch_predicate_is_preview_predicate_with_cls_columns():
    """The batch is not a second implementation: the same text, only two binds are replaced."""
    batch = preview_service._batch_where_a()
    restored = batch.replace("c.cls_include", "CAST(:cls_include AS jsonb)").replace(
        "c.cls_exclude", "CAST(:cls_exclude AS jsonb)"
    )
    assert restored == PREVIEW_WHERE_A


def test_batch_predicate_has_no_leftover_cls_binds():
    assert ":cls_" not in preview_service._batch_where_a()


def test_batch_predicate_fails_loudly_if_bind_renamed(monkeypatch):
    """If the bind is renamed in matching — it fails here, rather than silently counting its own."""
    monkeypatch.setattr(preview_service, "PREVIEW_WHERE_A", "tn.id IS NOT NULL")
    with pytest.raises(RuntimeError):
        preview_service._batch_where_a()


# --------------------------------------------------------------------------- #
# The numbers match the form preview
# --------------------------------------------------------------------------- #


def _batch_rows(n_payloads):
    """We emulate stage A “let everything through”: each row — to each card."""
    rows = []
    for idx in range(n_payloads):
        rows.extend(_rows(idx))
    return rows


@pytest.mark.asyncio
async def test_card_count_equals_form_preview_count():
    payloads = [template_prefill.from_template(t, source_keys=SRC) for t in TEMPLATES]
    payloads.append(template_prefill.unfiltered_payload(source_keys=SRC))

    batch_session = FakeSession(results=[FakeResult(items=_batch_rows(len(payloads)))])
    batch = await preview_service.preview_counts(batch_session, payloads)

    single = []
    for payload in payloads:
        session = FakeSession(results=[FakeResult(items=_rows())])
        single.append((await preview_service.preview_count(session, payload)).count)

    assert batch == single


@pytest.mark.asyncio
async def test_template_prefill_matches_serbian_tenders():
    """Acceptance #1: a profile from a template gives a preview > 0 (it was 0 — Russian words)."""
    payload = template_prefill.from_template(TEMPLATES[1], source_keys=SRC)
    session = FakeSession(results=[FakeResult(items=_rows())])
    assert (await preview_service.preview_count(session, payload)).count > 0


@pytest.mark.asyncio
async def test_template_without_dictionary_matches_nothing():
    """Mutation of the same profile: remove the dictionary — no matches remain."""
    payload = {
        **template_prefill.from_template(TEMPLATES[1], source_keys=SRC),
        "keyword_dict": False,
    }
    session = FakeSession(results=[FakeResult(items=_rows())])
    assert (await preview_service.preview_count(session, payload)).count == 0


@pytest.mark.asyncio
async def test_delivery_channels_does_not_change_preview_count():
    """profile_channels / step 1: inheriting the live channels in the prefill adds the key ``delivery_channels`` to the same payload that the ``/library`` showcase and the form preview count. The task's invariant — the figure “≈ N in 30 days” must stay the same: ``preview_service`` does not read the channels at all (``PREVIEW_WHERE_A`` / ``keywords_match`` know nothing about them — fact: 0 mentions of ``delivery_channels`` in ``src/api/cabinet/preview_service.py``).
    """
    payload = template_prefill.from_template(TEMPLATES[1], source_keys=SRC)
    before_session = FakeSession(results=[FakeResult(items=_rows())])
    before = (await preview_service.preview_count(before_session, payload)).count

    payload_with_channels = {**payload, "delivery_channels": ["telegram", "email"]}
    after_session = FakeSession(results=[FakeResult(items=_rows())])
    after = (
        await preview_service.preview_count(after_session, payload_with_channels)
    ).count

    assert before == after > 0


@pytest.mark.asyncio
async def test_unfiltered_gives_more_than_any_template():
    """Acceptance #3: “no filter” is noticeably wider than any template."""
    payloads = [template_prefill.from_template(t, source_keys=SRC) for t in TEMPLATES]
    payloads.append(template_prefill.unfiltered_payload(source_keys=SRC))
    session = FakeSession(results=[FakeResult(items=_batch_rows(len(payloads)))])
    counts = await preview_service.preview_counts(session, payloads)
    assert counts[-1] == len(CORPUS)
    assert counts[-1] > max(counts[:-1])


@pytest.mark.asyncio
async def test_batch_uses_single_query_for_uniform_cards():
    payloads = [template_prefill.from_template(t, source_keys=SRC) for t in TEMPLATES]
    session = FakeSession(results=[FakeResult(items=_batch_rows(len(payloads)))])
    await preview_service.preview_counts(session, payloads)
    queries = [q for q, _ in session.executed if "set_config" not in q]
    assert len(queries) == 1


# --------------------------------------------------------------------------- #
# Cache and robustness of the showcase
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_counts_are_cached_between_requests():
    library_counts.clear_cache()
    payloads = [template_prefill.from_template(TEMPLATES[0], source_keys=SRC)]
    first = FakeSession(results=[FakeResult(items=_batch_rows(1))])
    assert await library_counts.counts_for(first, payloads) == [1]

    second = FakeSession(results=[])  # there must be no request
    assert await library_counts.counts_for(second, payloads) == [1]
    assert not [q for q, _ in second.executed if "set_config" not in q]


@pytest.mark.asyncio
async def test_counts_failure_returns_none_not_zeros():
    """Lying with a zero is worse than not showing the number: the showcase distinguishes one from the other."""
    library_counts.clear_cache()

    class Boom(FakeSession):
        async def execute(self, statement, params=None):
            raise RuntimeError("db down")

    counts = await library_counts.counts_for(
        Boom(), [template_prefill.unfiltered_payload(source_keys=SRC)]
    )
    assert counts is None


# --------------------------------------------------------------------------- #
# Page
# --------------------------------------------------------------------------- #


def _client(*, sess=SESS):
    def factory():
        return FakeSession()

    app.dependency_overrides[deps.get_session_factory] = lambda: factory
    app.dependency_overrides[deps.get_optional_session] = lambda: sess
    return TestClient(app, follow_redirects=False)


def teardown_function():
    app.dependency_overrides.clear()
    library_counts.clear_cache()


def _async(return_value):
    async def _stub(*args, **kwargs):
        return return_value

    return _stub


def test_library_page_shows_live_count(monkeypatch):
    library_counts.clear_cache()
    monkeypatch.setattr(
        routes_library.svc, "list_public_templates", _async([TEMPLATES[1]])
    )
    n_cards = 1 + len(vertical_packages.PACKAGES) + 1
    monkeypatch.setattr(
        routes_library.library_counts, "counts_for", _async([7] * n_cards)
    )
    resp = _client().get("/library")
    assert resp.status_code == 200
    assert "≈ 7 тендеров за 30 дней" in resp.text
    assert "tpl-card__count" in resp.text


def test_library_page_shows_honest_zero(monkeypatch):
    library_counts.clear_cache()
    monkeypatch.setattr(
        routes_library.svc, "list_public_templates", _async([TEMPLATES[1]])
    )
    n_cards = 1 + len(vertical_packages.PACKAGES) + 1
    monkeypatch.setattr(
        routes_library.library_counts, "counts_for", _async([0] * n_cards)
    )
    resp = _client().get("/library")
    assert "пока нет совпадений" in resp.text
    assert "tpl-card__count--zero" in resp.text


def test_library_page_renders_without_counts(monkeypatch):
    """The counter failed — the showcase must open (without numbers)."""
    library_counts.clear_cache()
    monkeypatch.setattr(
        routes_library.svc, "list_public_templates", _async([TEMPLATES[1]])
    )
    monkeypatch.setattr(routes_library.library_counts, "counts_for", _async(None))
    resp = _client().get("/library")
    assert resp.status_code == 200
    assert "tpl-card__count" not in resp.text
    assert "Программное обеспечение" in resp.text


def test_library_page_offers_unfiltered_entry(monkeypatch):
    library_counts.clear_cache()
    monkeypatch.setattr(routes_library.svc, "list_public_templates", _async([]))
    monkeypatch.setattr(
        routes_library.library_counts,
        "counts_for",
        _async([1841] + [1] * len(vertical_packages.PACKAGES)),
    )
    resp = _client().get("/library")
    assert "Без фильтра — показывать все" in resp.text
    assert "/profiles/new/confirm?preset=all" in resp.text
    # an honest order-of-magnitude figure next to the button
    assert "≈ 1841 тендер за 30 дней" in resp.text


def test_library_counts_order_matches_cards(monkeypatch):
    """The order of the payloads = the order of the cards: a shift would attach someone else's number."""
    library_counts.clear_cache()
    captured = {}

    async def _capture(db, payloads):
        captured["payloads"] = payloads
        return list(range(len(payloads)))

    monkeypatch.setattr(
        routes_library.svc, "list_public_templates", _async([TEMPLATES[0]])
    )
    monkeypatch.setattr(routes_library.library_counts, "counts_for", _capture)
    _client().get("/library")

    payloads = captured["payloads"]
    assert payloads[0]["keywords_include"] == []  # “no filter” — the first card
    assert len(payloads) == 1 + len(vertical_packages.PACKAGES) + 1
    assert payloads[-1]["name"] == TEMPLATES[0].name


def test_batch_window_selects_every_predicate_column():
    """The batch window selects every column ``tn.*`` of the stage A predicate.

    In the batch ``tn`` is the CTE ``win``, not a table: a column that PREVIEW_WHERE_A reads but the window does not select brings the whole query down (prod 01.10: ``column tn.country_code does not exist`` → none of the /library cards had a number). Tests with FakeSession do not execute SQL — hence the check by text.
    """
    import re

    sql = str(preview_service._BATCH_SQL)
    window = sql[sql.index("AS MATERIALIZED (") : sql.index("FROM tenders_normalized")]
    selected = set(re.findall(r"\btn\.(\w+)", window))
    used = set(re.findall(r"\btn\.(\w+)", PREVIEW_WHERE_A))
    assert used, "the predicate without tn.* columns — the regexp is broken"
    assert used <= selected, f"the window win does not select: {sorted(used - selected)}"
