"""Step-wise AI analysis (src/ai/stepwise.py): Scout, splitting, critic, merge, accounting.

Not a single real model call: the client is a stub with ``analyze(request)``.
Expectations are literals from the fixtures (the number of chunks/tokens counted by hand), not via the code.
"""

from __future__ import annotations

import dataclasses

import pytest

from src.ai import prompts, stepwise
from src.ai.client import (
    AiResponseError,
    AiRetryableError,
    AnalysisRequest,
    AnalysisResult,
    Spend,
)
from src.ai.stepwise import (
    Chunk,
    Critic,
    Doc,
    analyze_tender,
    chunk_text,
    critic_filter,
    plan_chunks,
    scout,
    select_chunks,
    split_documents,
    stepwise_mode,
)

# --- fixtures ---------------------------------------------------------------- #

QUOTE_A = "Ponuđač mora da dostavi bankarsku garanciju za ozbiljnost ponude"
QUOTE_B = "Rok za dostavljanje ponuda je 15.11.2026. do 10:00 časova"


def _doc_text(n: int, quote: str) -> str:
    return (
        f"Opšti uslovi i opis predmeta nabavke. {quote}. " + "Tekst dokumenta. " * n
    )[: max(n, len(quote) + 60)]


def _glued(*docs: tuple[str, str]) -> str:
    return "\n\n".join(f"### {name}\n{text}" for name, text in docs)


def _result(output, tin=1000, tout=100, cost=0.01) -> AnalysisResult:
    return AnalysisResult(
        output=output,
        model="m",
        tokens_input=tin,
        tokens_output=tout,
        tokens_cached=0,
        cost_usd=cost,
    )


class FakeClient:
    """AiClient stub: extract answers by script, everything else — empty output."""

    def __init__(self, extract_outputs=None, extract_errors=None):
        self.calls: list[AnalysisRequest] = []
        self._extract = list(extract_outputs or [])
        self._errors = dict(extract_errors or {})

    async def analyze(self, request, **kwargs):
        self.calls.append(request)
        if request.level == "extract":
            i = sum(1 for c in self.calls if c.level == "extract") - 1
            if i in self._errors:
                raise self._errors[i]
            out = self._extract[i] if i < len(self._extract) else {"requirements": []}
            return _result(out, tin=2000, tout=200, cost=0.002)
        return _result({"summary": "итог"}, cost=0.1)


async def _noop(tokens, cost):  # pragma: no cover - replaced in tests
    return None


class Recorder:
    def __init__(self):
        self.items: list[tuple[int, float]] = []

    async def __call__(self, tokens, cost):
        self.items.append((tokens, cost))


# --- Scout ------------------------------------------------------------------- #


def test_scout_thresholds_chars():
    assert not scout("x" * 40_000, mode="auto").stepwise
    d = scout("x" * 40_001, mode="auto")
    assert d.stepwise and d.reason == "too_long"


def test_scout_thresholds_docs():
    four = _glued(*[(f"d{i}", "t") for i in range(4)])
    five = _glued(*[(f"d{i}", "t") for i in range(5)])
    assert not scout(four, mode="auto").stepwise
    d = scout(five, mode="auto")
    assert d.stepwise and d.reason == "many_docs" and d.docs == 5


def test_scout_off_on_empty_and_unreadable_share():
    big = "x" * 50_000
    assert (
        scout(big, mode="off").reason == "off" and not scout(big, mode="off").stepwise
    )
    assert scout("коротко", mode="on").stepwise
    assert not scout("   ", mode="on").stepwise
    docs = [
        {"extracted": "ok"},
        {"extracted": "scanned"},
        {"extracted": "error"},
        {"extracted": "ok"},
    ]
    d = scout("t", docs, mode="auto")
    assert (d.docs_listed, d.unreadable, d.unreadable_share) == (4, 2, 0.5)


def test_mode_from_env():
    assert stepwise_mode({}) == "off"
    assert stepwise_mode({"AI_STEPWISE": ""}) == "off"
    assert stepwise_mode({"AI_STEPWISE": "OFF"}) == "off"
    assert stepwise_mode({"AI_STEPWISE": "что-то"}) == "off"
    assert stepwise_mode({"AI_STEPWISE": "on"}) == "on"
    assert stepwise_mode({"AI_STEPWISE": " Auto "}) == "auto"


# --- splitting ---------------------------------------------------------------- #


def test_split_documents_by_markers():
    docs = split_documents("шапка\n\n### a.pdf\nтекст A\n\n### b.docx\nтекст B")
    assert [(d.name, d.text) for d in docs] == [
        ("", "шапка"),
        ("a.pdf", "текст A"),
        ("b.docx", "текст B"),
    ]
    assert split_documents("без маркеров")[0].name == ""
    assert split_documents("  ") == []


def test_chunk_text_size_overlap_and_coverage():
    text = "\n".join(f"строка {i:05d} " + "я" * 90 for i in range(1000))  # ≈ 100k
    parts = chunk_text(text, 30_000, 1_500)
    assert len(parts) == 4
    assert all(len(p) <= 30_000 for p in parts)
    # overlap: the tail of the previous one is at the start of the next
    assert parts[0][-200:].strip().split("\n")[-1] in parts[1]
    # coverage: every line got into at least one chunk
    joined = "\n".join(parts)
    assert all(f"строка {i:05d}" in joined for i in range(0, 1000, 37))
    assert chunk_text("коротко") == ["коротко"]


def test_plan_chunks_packs_small_docs_and_splits_big():
    small = [Doc(f"s{i}", "т" * 2_000) for i in range(12)]
    packed = plan_chunks(small)
    assert len(packed) == 1 and packed[0].docs == tuple(f"s{i}" for i in range(12))
    big = Doc("big.pdf", "я" * 70_000)
    plan = plan_chunks([small[0], big, small[1]])
    # batch [s0] | big×3 | batch [s1]
    assert [(c.docs, c.index, c.of) for c in plan] == [
        (("s0",), 1, 1),
        (("big.pdf",), 1, 3),
        (("big.pdf",), 2, 3),
        (("big.pdf",), 3, 3),
        (("s1",), 1, 1),
    ]


def test_select_chunks_caps_with_heads_first():
    plan = [Chunk((f"d{d}",), i, 3, "t") for d in range(10) for i in (1, 2, 3)]  # 30
    chosen = select_chunks(plan, limit=20)
    assert len(chosen) == 20
    assert {c.docs[0] for c in chosen} == {
        f"d{d}" for d in range(10)
    }  # each one has a head
    assert [c for c in chosen] == [c for c in plan if c in chosen]  # order preserved
    assert len(select_chunks(plan[:5], limit=20)) == 5


# --- critic ------------------------------------------------------------------ #


def _chunk(*names):
    return Chunk(tuple(names), 1, 1, "")


def test_critic_keeps_found_quote_drops_invented():
    critic = Critic({"a.pdf": f"Начало. {QUOTE_A}. Конец."})
    raw = [
        {"kind": "guarantee", "text": "Банк. гарантия", "quote": QUOTE_A},
        {"kind": "deadline", "text": "Срок 15.11", "quote": QUOTE_B},  # fabricated
    ]
    kept, dropped = critic_filter(raw, _chunk("a.pdf"), critic, set())
    assert dropped == 1
    assert [(r["kind"], r["doc"], r["chunk"]) for r in kept] == [
        ("guarantee", "a.pdf", 1)
    ]


def test_critic_normalizes_case_whitespace_script_and_quotes():
    critic = Critic(
        {"a": "Понуђач мора да достави\n  банкарску   гаранцију за озбиљност понуде"}
    )
    lat = "„Ponuđač MORA da dostavi bankarsku garanciju za ozbiljnost ponude“."
    kept, dropped = critic_filter(
        [{"kind": "guarantee", "text": "x", "quote": lat}], _chunk("a"), critic, set()
    )
    assert (len(kept), dropped) == (1, 0)


def test_critic_rejects_short_quote_and_empty_text():
    critic = Critic({"a": "rok je 5 dana, rok je 5 dana"})
    raw = [
        {
            "kind": "deadline",
            "text": "t",
            "quote": "rok",
        },  # shorter than the threshold — not proof
        {"kind": "deadline", "text": "", "quote": "rok je 5 dana"},
    ]
    kept, dropped = critic_filter(raw, _chunk("a"), critic, set())
    assert (kept, dropped) == ([], 2)


def test_critic_assigns_real_doc_in_pack_and_dedups_overlap():
    critic = Critic({"a": "ничего", "b": f"тут есть {QUOTE_A} и всё"})
    item = {
        "kind": "ЛИЦЕНЗИЯ?",
        "text": "t",
        "quote": QUOTE_A,
        "doc": "a",
    }  # the model named the wrong doc
    seen: set = set()
    kept, _ = critic_filter([item], _chunk("a", "b"), critic, seen)
    assert (kept[0]["doc"], kept[0]["kind"]) == ("b", "other")
    again, dropped = critic_filter([item], _chunk("a", "b"), critic, seen)
    assert (again, dropped) == ([], 0)  # a duplicate from the overlap — not "dropped"


# --- orchestration ------------------------------------------------------------- #


def _big_request(level="deep", docs=3, per_doc=45_000):
    text = _glued(
        *[
            (f"doc{i}.pdf", _doc_text(per_doc, QUOTE_A if i == 0 else QUOTE_B))
            for i in range(docs)
        ]
    )
    return AnalysisRequest(
        level=level, title="Tender", description=text, target_language="ru"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("level", ["deep", "checklist"])
async def test_synthesis_gets_journal_not_raw_text_and_usage_per_step(level):
    req = _big_request(level)
    fake = FakeClient(
        [
            {
                "requirements": [
                    {"kind": "guarantee", "text": "Гарантия", "quote": QUOTE_A}
                ]
            },
            {},
            {
                "requirements": [
                    {"kind": "deadline", "text": "Срок", "quote": QUOTE_B},
                    {
                        "kind": "other",
                        "text": "выдумка",
                        "quote": "этой фразы в документах нет совсем",
                    },
                ]
            },
        ]
    )
    rec = Recorder()
    result = await analyze_tender(fake, req, record_spend=rec, mode="auto")

    steps = [c for c in fake.calls if c.level == "extract"]
    final = [c for c in fake.calls if c.level == level]
    assert len(steps) == 6  # 3 documents × 2 chunks (45k > 30k)
    assert len(final) == 1
    # the merge sees the start of the text (subject/TOR) + the journal, not the whole raw text
    desc = final[0].description
    assert desc.startswith("НАЧАЛО ДОКУМЕНТАЦИИ")
    assert req.description.strip()[:8_000] in desc
    assert req.description.strip()[:8_001] not in desc
    assert "ЖУРНАЛ ТРЕБОВАНИЙ" in desc and QUOTE_A in desc
    assert len(desc) < 45_000
    # accounting: EVERY step is recorded (2000+200 tokens, $0.002), the merge — on the caller
    assert rec.items == [(2200, 0.002)] * 6
    meta = result.output["stepwise"]
    assert (meta["chunks_total"], meta["chunks_done"], meta["dropped_by_critic"]) == (
        6,
        6,
        1,
    )
    assert meta["steps_cost_usd"] == pytest.approx(0.012)
    assert [r["doc"] for r in result.output["requirements"]] == ["doc0.pdf", "doc1.pdf"]
    assert result.output["summary"] == "итог"
    assert (
        result.cost_usd == 0.1
    )  # only the merge; the steps are already recorded separately


@pytest.mark.asyncio
async def test_step_sees_only_its_chunk_and_short_summary():
    req = _big_request("deep", docs=2)
    fake = FakeClient(
        [
            {
                "requirements": [
                    {"kind": "guarantee", "text": "Гарантия", "quote": QUOTE_A}
                ]
            }
        ]
    )
    await analyze_tender(fake, req, record_spend=Recorder(), mode="auto")
    steps = [c for c in fake.calls if c.level == "extract"]
    assert all(len(s.description) <= 30_000 for s in steps)
    assert steps[0].extra_context is None
    assert "guarantee (1): Гарантия" in steps[1].extra_context
    assert len(steps[1].extra_context) <= stepwise.SUMMARY_MAX_CHARS


@pytest.mark.asyncio
async def test_small_tender_goes_the_normal_way_and_off_mode():
    small = AnalysisRequest(level="deep", title="T", description="коротко")
    fake = FakeClient()
    await analyze_tender(fake, small, record_spend=Recorder(), mode="auto")
    assert [c.level for c in fake.calls] == ["deep"] and fake.calls[0] is small
    big, fake2 = _big_request(), FakeClient()
    await analyze_tender(fake2, big, record_spend=Recorder(), mode="off")
    assert [c.level for c in fake2.calls] == ["deep"]
    # levels outside deep/checklist never go step-wise
    std = dataclasses.replace(big, level="standard")
    fake3 = FakeClient()
    await analyze_tender(fake3, std, record_spend=Recorder(), mode="on")
    assert [c.level for c in fake3.calls] == ["standard"]


@pytest.mark.asyncio
async def test_step_cap_marks_partial_coverage():
    # 12 documents of 80k → 3 chunks each = 36 chunks > ceiling 20
    req = _big_request("deep", docs=12, per_doc=80_000)
    rec = Recorder()
    fake = FakeClient(
        [{"requirements": [{"kind": "other", "text": "т", "quote": QUOTE_B}]}] * 30
    )
    result = await analyze_tender(fake, req, record_spend=rec, mode="auto")
    assert (
        len([c for c in fake.calls if c.level == "extract"]) == stepwise.MAX_STEPS == 20
    )
    assert len(rec.items) == 20
    meta = result.output["stepwise"]
    assert (meta["chunks_done"], meta["chunks_total"]) == (20, 36)


@pytest.mark.asyncio
async def test_failed_step_spend_recorded_and_all_failed_raises():
    req = _big_request("deep", docs=2)
    boom = AiResponseError("плохой JSON", spend=Spend(tokens=900, cost_usd=0.004))
    fake = FakeClient(
        [{}, {"requirements": [{"kind": "guarantee", "text": "Г", "quote": QUOTE_A}]}],
        extract_errors={0: boom},
    )
    rec = Recorder()
    result = await analyze_tender(fake, req, record_spend=rec, mode="auto")
    assert rec.items[0] == (900, 0.004)  # the paid failed step is recorded
    assert result.output["stepwise"]["chunks_failed"] == 1

    all_bad = FakeClient(extract_errors={i: AiResponseError("x") for i in range(10)})
    with pytest.raises(AiResponseError):
        await analyze_tender(all_bad, req, record_spend=Recorder(), mode="auto")
    assert not [
        c for c in all_bad.calls if c.level == "deep"
    ]  # the premium was not called


@pytest.mark.asyncio
async def test_retryable_error_propagates_after_recording_done_steps():
    req = _big_request("deep", docs=2)
    fake = FakeClient([{}], extract_errors={1: AiRetryableError("429")})
    rec = Recorder()
    with pytest.raises(AiRetryableError):
        await analyze_tender(fake, req, record_spend=rec, mode="auto")
    assert len(rec.items) == 1


@pytest.mark.asyncio
async def test_empty_journal_falls_back_to_normal_analysis():
    req = _big_request("checklist", docs=2)
    fake = FakeClient([])  # the steps found nothing
    result = await analyze_tender(fake, req, record_spend=Recorder(), mode="auto")
    assert (
        fake.calls[-1] is req
        and "requirements" not in result.output
        or not result.output.get("requirements")
    )


# --- regular prompts: 50k ceiling ------------------------------------------------ #


def test_normal_prompts_cap_300k_description_at_50k():
    desc = "я" * 300_000
    for text in (
        prompts.build_l2_user("T", desc, None, None),
        prompts.build_l3_user("T", desc),
        prompts.build_bid_user("T", desc, None),
        prompts.build_checklist_user("T", desc, None),
        prompts.build_draft_user("T", desc, None),
    ):
        assert desc[:50_000] in text and desc[:50_001] not in text
        assert len(text) < 50_200
    assert prompts.cap_description(None) == ""


def test_description_display_capped_at_50k():
    from src.domain.description_render import split_description_blocks

    blocks = split_description_blocks("### f.pdf\n" + "я" * 300_000)
    assert sum(len(b.text) for b in blocks) == 50_000 - len("### f.pdf\n")
