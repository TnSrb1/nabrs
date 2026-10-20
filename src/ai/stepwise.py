"""Step-wise (Markovian) AI analysis of tender documentation — levels ``deep`` and ``checklist``.

Why. A regular analysis gives the model the whole tender text as one string, and the prompt cuts it to 50k characters (:data:`src.ai.prompts.PROMPT_DESCRIPTION_MAX_CHARS`). Measurement 19.10.2026 (active jnportal): 371 of 1902 (20%) hit 50k, p90 of text 183k, max 502k — the tail of the documentation was never seen by the model. Here the text is read IN FULL, but in chunks:

1. **Scout** (no model): :func:`scout` decides whether step-wise mode is needed.
2. **Splitting**: by documents (``### name``); a large document — into chunks ≤ :data:`CHUNK_CHARS` with overlap :data:`CHUNK_OVERLAP`.
3. **Steps** (cheap/haiku, level ``extract``): chunk + short summary of what has been found (Markov state — NOT the whole journal) → JSON of requirements ``{kind, text, quote}``.
4. **Critic** (no model): a requirement stays only if its ``quote`` is found verbatim (after folding via :mod:`src.domain.text_norm` and collapsing spaces) in the text of its own document. Dropped ones — a counter in the output.
5. **Merge**: a call of the current tier of the level (deep=premium, checklist=balanced) on the JOURNAL, not on the raw text; the previous output format + ``requirements`` (the journal, written by CODE, not by the model) + ``stepwise`` (coverage).

Money (``.claude/rules/money-ai-tiers.md``). The ``limit_decision`` gate by level stays with the caller BEFORE entry here and does not change: one analysis = one unit of limit. Each sub-call of a step is written to ``ai_usage`` IMMEDIATELY via ``record_spend`` (calls=0: does not touch the limit unit, records actual tokens and price); the merge — as before, via the caller's ``finalize_with_insert`` (calls=1). The ceiling — :data:`MAX_STEPS` steps per analysis; beyond it, an honest mark ``chunks_done < chunks_total``.

Mode — env ``AI_STEPWISE``: ``off`` (default, as before) / ``auto`` (Scout decides) / ``on`` (forced, for checks).
"""

from __future__ import annotations

import dataclasses
import logging
import os
import re
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Mapping, Sequence

from src.ai.client import (
    AiClient,
    AiResponseError,
    AiRetryableError,
    AnalysisRequest,
    AnalysisResult,
)
from src.ai.schemas import REQUIREMENT_KINDS
from src.domain.text_norm import fold

logger = logging.getLogger(__name__)

ENV_FLAG = "AI_STEPWISE"
MODE_AUTO, MODE_OFF, MODE_ON = "auto", "off", "on"

# Levels that have a step-wise mode (the others go the regular way).
STEPWISE_LEVELS = frozenset({"deep", "checklist"})

# --- Scout thresholds -------------------------------------------------------------
# Measurement 19.10.2026 (SQL, owner, active jnportal): 371/1902 (20%) of texts hit
# 50k (former storage ceiling), p90 = 183k, max = 502k. The regular prompt sees ≤ 50k;
# 40k — with a margin below, so that tenders near the boundary do not lose the tail. Documents > 4 —
# multi-file documentation (technical spec + terms of reference + samples + contract), where the key
# is scattered across files and "gluing into one string" drowns it.
STEPWISE_MIN_CHARS = 40_000
STEPWISE_MIN_DOCS = 4  # step-wise when READABLE documents are STRICTLY more than this

# --- splitting -----------------------------------------------------------------
CHUNK_CHARS = 30_000
CHUNK_OVERLAP = 1_500
# Ceiling on steps per analysis. Guarantees a bounded cost: ≤ 20 × cheap call (~30k
# chars ≈ 10k tokens) + one merge. Beyond it — "partially analysed: N of M".
MAX_STEPS = 20

# --- critic / journal ----------------------------------------------------------
# A quote shorter than this (after folding) matches anything — but is not proof.
QUOTE_MIN_CHARS = 12
TEXT_MAX_CHARS = 400
QUOTE_MAX_CHARS = 300
MAX_REQUIREMENTS = 300  # record cap for journal entries in the output
# Merge input ≤ 45k characters: journal + start of the source text (subject of procurement/TOR,
# whence the "subject / volume of work" fields come, which are not in the requirements journal).
SYNTHESIS_MAX_CHARS = 45_000
HEAD_MAX_CHARS = 8_000
JOURNAL_MAX_CHARS = SYNTHESIS_MAX_CHARS - HEAD_MAX_CHARS - 200  # 200 — block header
SUMMARY_MAX_CHARS = 1_500  # the "summary of findings" the step sees
SUMMARY_ITEMS_PER_KIND = 4

_DOC_MARK = re.compile(r"(?m)^### (.*)$")
_WS = re.compile(r"\s+")
_QUOTE_EDGE = " \t\r\n\"'«»“”„‘’`.…-–—[]()"

RecordSpend = Callable[[int, float], Awaitable[None]]


# --------------------------------------------------------------------------- #
#  Mode and Scout
# --------------------------------------------------------------------------- #


def stepwise_mode(env: Mapping[str, str] | None = None) -> str:
    """Mode from env ``AI_STEPWISE``: ``off`` (default) / ``auto`` / ``on``.

    Empty or unknown value → ``off``: the paid mode is enabled only explicitly.
    """
    raw = (env if env is not None else os.environ).get(ENV_FLAG, "")
    value = raw.strip().lower()
    if value == MODE_AUTO:
        return MODE_AUTO
    if value in (MODE_ON, "1", "true", "yes"):
        return MODE_ON
    return MODE_OFF


@dataclass(frozen=True)
class ScoutDecision:
    """Scout decision: mode and the figures it was made on."""

    stepwise: bool
    reason: str  # off | forced | too_long | many_docs | small | empty
    chars: int
    docs: int  # readable documents (``### `` markers in the text)
    docs_listed: int  # documents in the ``documents`` list (0 — no list)
    unreadable: int  # in the list with status extracted != ok
    unreadable_share: float


def scout(
    text: str | None,
    documents: Sequence[Mapping[str, Any]] | None = None,
    *,
    mode: str | None = None,
) -> ScoutDecision:
    """Decides whether step-wise mode is needed. No model, no DB."""
    body = text or ""
    docs = len(_DOC_MARK.findall(body))
    listed = len(documents or [])
    unreadable = sum(1 for d in (documents or []) if (d or {}).get("extracted") != "ok")
    share = round(unreadable / listed, 3) if listed else 0.0

    def decision(stepwise: bool, reason: str) -> ScoutDecision:
        return ScoutDecision(
            stepwise, reason, len(body), docs, listed, unreadable, share
        )

    mode = mode or stepwise_mode()
    if mode == MODE_OFF:
        return decision(False, "off")
    if not body.strip():
        return decision(False, "empty")
    if mode == MODE_ON:
        return decision(True, "forced")
    if len(body) > STEPWISE_MIN_CHARS:
        return decision(True, "too_long")
    if docs > STEPWISE_MIN_DOCS:
        return decision(True, "many_docs")
    return decision(False, "small")


# --------------------------------------------------------------------------- #
#  Splitting
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Doc:
    name: str
    text: str


@dataclass(frozen=True)
class Chunk:
    """Analysis step: a chunk of ONE large document OR a batch of small documents.

    ``docs`` — names of documents whose text is in ``text`` (a batch has several, the text —
    with ``### name`` headers); ``index``/``of`` — number of the chunk within the document
    (a batch is 1 of 1).
    """

    docs: tuple[str, ...]
    index: int
    of: int
    text: str

    @property
    def label(self) -> str:
        names = ", ".join(n or "—" for n in self.docs)
        if len(names) > 120:
            names = names[:117] + "..."
        part = f" — кусок {self.index} из {self.of}" if self.of > 1 else ""
        return f"{names}{part}"


def split_documents(text: str) -> list[Doc]:
    """Splits the concatenated text by ``### name`` markers. Without markers — one document."""
    marks = list(_DOC_MARK.finditer(text))
    if not marks:
        return [Doc("", text.strip())] if text.strip() else []
    docs: list[Doc] = []
    head = text[: marks[0].start()].strip()
    if head:
        docs.append(Doc("", head))
    for i, m in enumerate(marks):
        end = marks[i + 1].start() if i + 1 < len(marks) else len(text)
        body = text[m.end() : end].strip()
        if body:
            docs.append(Doc(m.group(1).strip(), body))
    return docs


def chunk_text(
    text: str, size: int = CHUNK_CHARS, overlap: int = CHUNK_OVERLAP
) -> list[str]:
    """Chunks ≤ ``size`` with overlap ``overlap``; the boundary is searched for at a line break."""
    if len(text) <= size:
        return [text]
    out: list[str] = []
    start = 0
    while start < len(text):
        end = min(start + size, len(text))
        if end < len(text):
            nl = text.rfind("\n", start + size - size // 5, end)
            if nl > start:
                end = nl
        out.append(text[start:end])
        if end >= len(text):
            break
        start = max(end - overlap, start + 1)
    return out


def _with_header(d: Doc) -> str:
    return f"### {d.name}\n{d.text}" if d.name else d.text


def plan_chunks(docs: Sequence[Doc]) -> list[Chunk]:
    """Analysis steps in document order.

    A large document (> :data:`CHUNK_CHARS`) is split into chunks with overlap; small
    documents in a row are packed into batches ≤ :data:`CHUNK_CHARS`: 12 certificates of 2k
    characters — one step, not twelve (the cost of a step does not depend on the number of files).
    """
    chunks: list[Chunk] = []
    pack: list[Doc] = []
    size = 0

    def flush() -> None:
        nonlocal pack, size
        if pack:
            text = "\n\n".join(_with_header(d) for d in pack)
            chunks.append(Chunk(tuple(d.name for d in pack), 1, 1, text))
        pack, size = [], 0

    for d in docs:
        if len(d.text) > CHUNK_CHARS:
            flush()
            parts = chunk_text(d.text)
            chunks.extend(
                Chunk((d.name,), i + 1, len(parts), p) for i, p in enumerate(parts)
            )
            continue
        need = len(_with_header(d)) + 2
        if pack and size + need > CHUNK_CHARS:
            flush()
        pack.append(d)
        size += need
    flush()
    return chunks


def select_chunks(chunks: Sequence[Chunk], limit: int = MAX_STEPS) -> list[Chunk]:
    """No more than ``limit`` chunks: first the heads of documents (round-robin), then the tails.

    So when the ceiling overflows, each document gets to contribute its beginning (usually
    the participation conditions are there), rather than the first two files eating the whole budget. The return order
    is the original (document after document): the Markov summary reads naturally.
    """
    if len(chunks) <= limit:
        return list(chunks)
    ranked = sorted(range(len(chunks)), key=lambda i: (chunks[i].index, i))[:limit]
    return [chunks[i] for i in sorted(ranked)]


# --------------------------------------------------------------------------- #
#  Critic (deterministic)
# --------------------------------------------------------------------------- #


def _norm(value: str) -> str:
    return _WS.sub(" ", fold(value)).strip()


def _norm_quote(value: str) -> str:
    return _norm(value.strip(_QUOTE_EDGE))


@dataclass
class Critic:
    """Checks quotes against the text of the documents. The folding of a document is computed once."""

    docs: Mapping[str, str]
    _folded: dict[str, str] = dataclasses.field(default_factory=dict)

    def _doc_norm(self, name: str) -> str:
        if name not in self._folded:
            self._folded[name] = _norm(self.docs.get(name, ""))
        return self._folded[name]

    def accepts(self, doc: str, quote: str) -> bool:
        needle = _norm_quote(quote or "")
        if len(needle) < QUOTE_MIN_CHARS:
            return False
        return needle in self._doc_norm(doc)


def critic_filter(
    raw: Sequence[Mapping[str, Any]],
    chunk: Chunk,
    critic: Critic,
    seen: set[tuple[str, str]],
) -> tuple[list[dict[str, Any]], int]:
    """(accepted requirements of the chunk, number dropped by the critic).

    ``doc`` is set by the CODE: the document of the step (from ``chunk.docs``) in whose text the quote is actually found (the name from the model is only a hint for the check order); ``chunk`` — chunk number. Duplicates (chunk overlap) are not counted as dropped — they are simply not repeated.
    """
    kept: list[dict[str, Any]] = []
    dropped = 0
    for item in raw:
        text = str(item.get("text") or "").strip()
        quote = str(item.get("quote") or "").strip()
        claimed = str(item.get("doc") or "").strip()
        order = sorted(chunk.docs, key=lambda n: n != claimed)  # the declared one first
        doc = next((n for n in order if critic.accepts(n, quote)), None)
        if not text or doc is None:
            dropped += 1
            continue
        kind = str(item.get("kind") or "other").strip().lower()
        if kind not in REQUIREMENT_KINDS:
            kind = "other"
        key = (kind, _norm_quote(quote))
        if key in seen:
            continue
        seen.add(key)
        kept.append(
            {
                "kind": kind,
                "text": text[:TEXT_MAX_CHARS],
                "quote": quote[:QUOTE_MAX_CHARS],
                "doc": doc,
                "chunk": chunk.index,
            }
        )
    return kept, dropped


# --------------------------------------------------------------------------- #
#  Summary (Markov state) and journal for the merge
# --------------------------------------------------------------------------- #


def found_summary(journal: Sequence[Mapping[str, Any]]) -> str:
    """Short summary of findings for the next step: kinds, counters, a pair of examples."""
    if not journal:
        return ""
    lines: list[str] = []
    for kind in REQUIREMENT_KINDS:
        items = [r for r in journal if r["kind"] == kind]
        if not items:
            continue
        shown = "; ".join(r["text"][:80] for r in items[-SUMMARY_ITEMS_PER_KIND:])
        lines.append(f"- {kind} ({len(items)}): {shown}")
    return "\n".join(lines)[:SUMMARY_MAX_CHARS]


def journal_for_synthesis(
    journal: Sequence[Mapping[str, Any]], meta: Mapping[str, Any]
) -> tuple[str, int]:
    """(journal text for the merge, how many entries did not fit the budget)."""
    header = (
        "ЖУРНАЛ ТРЕБОВАНИЙ (не сырой текст): требования, извлечённые по документам "
        "тендера и проверенные по дословным цитатам. Источник каждого — документ и "
        f"фрагмент. Разобрано кусков документации: {meta['chunks_done']} из "
        f"{meta['chunks_total']}."
    )
    if meta["chunks_done"] < meta["chunks_total"]:
        header += (
            " Документация разобрана ЧАСТИЧНО — не утверждай того, чего нет в журнале."
        )
    lines: list[str] = []
    used = len(header)
    omitted = 0
    for i, r in enumerate(journal, 1):
        line = (
            f'[{i}] {r["kind"]}: {r["text"]} (документ «{r["doc"] or "—"}», '
            f'фрагмент: "{r["quote"]}")'
        )
        if used + len(line) + 1 > JOURNAL_MAX_CHARS:
            omitted = len(journal) - i + 1
            break
        lines.append(line)
        used += len(line) + 1
    return header + "\n" + "\n".join(lines), omitted


def head_for_synthesis(text: str) -> str:
    """Start of the source text (≤ :data:`HEAD_MAX_CHARS`) for the merge: subject / TOR."""
    head = (text or "").strip()[:HEAD_MAX_CHARS]
    if not head:
        return ""
    return "НАЧАЛО ДОКУМЕНТАЦИИ (предмет закупки, объём работ):\n" + head


# --------------------------------------------------------------------------- #
#  Accounting of step spend
# --------------------------------------------------------------------------- #


def step_recorder(
    session: Any,
    *,
    tenant_id: str | None,
    level: str,
    after_commit: Callable[[], Awaitable[None]] | None = None,
) -> RecordSpend:
    """``record_spend`` for :func:`analyze_tender`: step → ``ai_usage`` (calls=0).

    ``record_usage`` does a commit, and ``app.current_tenant_id`` is transaction-local
    (``set_config(..., is_local)``): after the commit the RLS context would vanish, and the
    next step / ``finalize_with_insert`` would write to 0 rows (fail-closed).
    Therefore right after the write the tenant is set again in a NEW transaction;
    ``after_commit`` — so that the caller returns what the commit released (advisory lock).
    """
    from src.ai.usage import record_usage
    from src.db.session import set_current_tenant

    async def _record(tokens: int, cost: float) -> None:
        await record_usage(
            session, tenant_id=tenant_id, level=level, calls=0, tokens=tokens, cost=cost
        )
        if tenant_id is not None:
            await set_current_tenant(session, str(tenant_id))
        if after_commit is not None:
            await after_commit()

    return _record


# --------------------------------------------------------------------------- #
#  Orchestration
# --------------------------------------------------------------------------- #


async def analyze_tender(
    client: AiClient,
    request: AnalysisRequest,
    *,
    record_spend: RecordSpend,
    documents: Sequence[Mapping[str, Any]] | None = None,
    mode: str | None = None,
) -> AnalysisResult:
    """Single entry point for deep/checklist: Scout → regular path or step-wise.

    ``record_spend(tokens, cost_usd)`` is called after EVERY sub-call of a step
    (including a failed but paid one) — the caller writes this to ``ai_usage`` with
    ``calls=0``. Exceptions — as for :meth:`AiClient.analyze`.
    """
    if request.level not in STEPWISE_LEVELS:
        return await client.analyze(request)
    decision = scout(request.description, documents, mode=mode)
    logger.info(
        "stepwise: scout level=%s stepwise=%s reason=%s chars=%d docs=%d unreadable=%d/%d",
        request.level,
        decision.stepwise,
        decision.reason,
        decision.chars,
        decision.docs,
        decision.unreadable,
        decision.docs_listed,
    )
    if not decision.stepwise:
        return await client.analyze(request)
    return await run_stepwise(
        client,
        request,
        record_spend=record_spend,
        unreadable=decision.unreadable,
    )


async def run_stepwise(
    client: AiClient,
    request: AnalysisRequest,
    *,
    record_spend: RecordSpend,
    unreadable: int = 0,
) -> AnalysisResult:
    """Steps → critic → merge. Call via :func:`analyze_tender`."""
    docs = split_documents(request.description or "")
    planned = plan_chunks(docs)
    chunks = select_chunks(planned)
    texts: dict[str, str] = {}
    for d in docs:
        texts[d.name] = f"{texts[d.name]}\n{d.text}" if d.name in texts else d.text
    critic = Critic(texts)

    journal: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    done = failed = dropped = 0
    steps_cost = 0.0

    for n, chunk in enumerate(chunks, 1):
        label = f"{chunk.label} [{n}/{len(chunks)}]"
        step = AnalysisRequest(
            level="extract",
            title=request.title,
            description=chunk.text,
            target_language=request.target_language,
            extra_context=found_summary(journal) or None,
            step_label=label,
        )
        try:
            res = await client.analyze(step)
        except AiRetryableError:
            raise  # model busy: already paid steps are recorded, the caller will return model_busy
        except AiResponseError as exc:
            spend = getattr(exc, "spend", None)
            if spend is not None:
                steps_cost += spend.cost_usd
                await record_spend(spend.tokens, spend.cost_usd)
            failed += 1
            logger.warning("stepwise: step %d/%d not analysed (%s)", n, len(chunks), exc)
            continue
        await record_spend(res.tokens_input + res.tokens_output, res.cost_usd)
        steps_cost += res.cost_usd
        done += 1
        kept, drop = critic_filter(
            res.output.get("requirements") or [], chunk, critic, seen
        )
        dropped += drop
        journal.extend(kept)

    if done == 0:
        raise AiResponseError("stepwise: no chunk of documentation was analysed")
    if not journal:
        # Steps ran, but there are no requirements with a verifiable quote: the premium merge
        # would be analysing emptiness. The regular path (≤ 50k) is more honest.
        logger.info("stepwise: journal is empty → regular analysis level=%s", request.level)
        return await client.analyze(request)

    overflow = max(len(journal) - MAX_REQUIREMENTS, 0)
    journal = journal[:MAX_REQUIREMENTS]
    meta: dict[str, Any] = {
        "chunks_total": len(planned),
        "chunks_done": done,
        "chunks_failed": failed,
        "docs": len(docs),
        "dropped_by_critic": dropped,
        "unreadable_docs": unreadable,
        "journal_truncated": overflow,
        "steps_cost_usd": round(steps_cost, 6),
    }
    # chunks_total - chunks_done = chunks not taken because of the MAX_STEPS ceiling + failed steps.
    text, omitted = journal_for_synthesis(journal, meta)
    meta["journal_truncated"] = overflow + omitted
    head = head_for_synthesis(request.description or "")
    if head:
        text = f"{head}\n\n{text}"

    result = await client.analyze(dataclasses.replace(request, description=text))
    output = dict(result.output)
    output["requirements"] = journal
    output["stepwise"] = meta
    logger.info(
        "stepwise: level=%s chunks=%d/%d journal=%d dropped=%d steps_cost=$%.4f",
        request.level,
        done,
        len(planned),
        len(journal),
        dropped,
        steps_cost,
    )
    return dataclasses.replace(result, output=output)
