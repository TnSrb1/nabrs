"""Stepwise: step accounting in ai_usage (mock) and display of the journal (bot, web macro)."""

from __future__ import annotations

import pytest

from src.ai import stepwise
from src.bots.requirements_render import render_requirements

KINDS = ["other", "deadline", "guarantee", "license", "experience", "staff"]


def _output(n=12, done=5, total=5):
    return {
        "requirements": [
            {
                "kind": KINDS[i % 6],
                "text": f"треб{i}",
                "quote": f"цитата номер {i}",
                "doc": f"f{i}.pdf",
                "chunk": 1,
            }
            for i in range(n)
        ],
        "stepwise": {"chunks_done": done, "chunks_total": total},
    }


@pytest.mark.asyncio
async def test_step_recorder_writes_calls_zero_and_restores_tenant(monkeypatch):
    log: list = []

    async def fake_record(session, **kw):
        log.append(("usage", kw))

    async def fake_tenant(session, tid):
        log.append(("tenant", tid))

    async def relock():
        log.append(("relock",))

    import src.ai.usage as usage
    import src.db.session as dbs

    monkeypatch.setattr(usage, "record_usage", fake_record)
    monkeypatch.setattr(dbs, "set_current_tenant", fake_tenant)
    rec = stepwise.step_recorder(
        object(), tenant_id="T1", level="deep", after_commit=relock
    )
    await rec(2200, 0.002)
    # calls=0: a step does not spend a unit of the limit; the tenant and the lock are returned AFTER the commit
    assert log == [
        (
            "usage",
            {
                "tenant_id": "T1",
                "level": "deep",
                "calls": 0,
                "tokens": 2200,
                "cost": 0.002,
            },
        ),
        ("tenant", "T1"),
        ("relock",),
    ]


def test_bot_block_shows_at_most_10_by_priority_and_site_hint():
    html = render_requirements(_output(12), "ru")
    assert html.count("  • ") == 10
    assert "Полный список (12) — на сайте." in html
    # deadline ranks above other: 10 places taken by deadline..staff, "other" did not fit
    assert "Срок:" in html and "Прочее:" not in html
    assert "частично" not in html


def test_bot_block_partial_note_and_absent_without_journal():
    assert "частично: 3 из 5" in render_requirements(_output(2, done=3, total=5), "ru")
    assert render_requirements({"summary": "x"}, "ru") is None
    assert "Requirements by document" in render_requirements(_output(2), "en")


def test_bot_block_escapes_html():
    out = _output(1)
    out["requirements"][0]["text"] = "<b>x</b>"
    assert "&lt;b&gt;x&lt;/b&gt;" in render_requirements(out, "ru")


def test_web_macro_renders_requirements_with_document_name():
    from src.api.cabinet.i18n import CABINET  # noqa: F401  (catalogs are connected)
    from src.api.templating import templates

    t = {
        "art_req_title": "Требования по документам",
        "art_req_doc": "Документ",
        "art_req_partial": "Частично: {done} из {total}.",
        "art_req_kind_deadline": "Срок",
        "art_req_kind_other": "Прочее",
    }
    mod = templates.env.get_template("_ai_artifact.html").make_module({"t": t})
    out = _output(2, done=3, total=5)
    html = str(mod.requirements_block(out))
    assert (
        "Требования по документам" in html
        and "f0.pdf" in html
        and "цитата номер 0" in html
    )
    assert "Частично: 3 из 5." in html
    assert str(mod.requirements_block({"summary": "x"})).strip() == ""
