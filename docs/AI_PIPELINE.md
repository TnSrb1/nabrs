# AI pipeline

How a tender goes through the AI layer, from its attached files to an analysis the customer can check.
Everything below is code in this repository; each stage names the function that does it.
Counts and thresholds are the values in the code on **20 October 2026**.

<img src="assets/ai-pipeline-animated.svg" alt="Animated AI pipeline: documents, text extraction, gate before the call, fast verdicts, step-by-step analysis with a quote critic, and the usage ledger" width="100%">

<sub>Animated: each stage is highlighted in turn. The text below follows the same order.</sub>

## 1. Documents become text

A tender usually comes with a bundle of files: the call for bids, a technical specification, forms, a draft contract.
The connector layer downloads up to **12 files** (at most **300,000 characters** of text in total) and extracts text
from `.pdf`, `.doc`, `.docx`, `.odt`, `.rtf`, `.xls`, `.xlsx`, `.txt` and `.zip` archives (members are unpacked with depth and size caps).
Each file gets one status: `ok`, `needs_ocr` (a scan without a text layer), `unsupported` or `error`.
Files that could not be read are counted and kept in the tender's document list, so the analysis can say how much it did not see.

The connectors are not published (see the [README](../README.md)); the contract they fulfil is `src/parsers/base.py`.
The text reaches the AI layer as one string where every file starts with a `### <file name>` marker.

## 2. The gate runs before any paid call

Each paid call has three parts: a level in the tier table, a gate **before** the call and a usage record **after** it.

- The table of monthly limits per plan is `_TIER_LIMITS` in `src/ai/usage.py`. `0` means the level is not on the plan, `None` means no limit (enterprise only).
- `limit_decision` returns `ok`, `unavailable` or `exhausted`. It is used by the button-triggered levels (`src/api/cabinet/tender_ai_generate.py`, `src/bots/*_service.py`).
- Automatic analysis of every delivered tender is gated again by the subscription status: `auto_limit_decision`. A trial gets a small demo allowance over the whole trial, not per month; a suspended or cancelled account gets nothing.
- A result that is already cached for the same tender and profile is returned without a charge (`cache_lookup`). A cached result is treated as stale when the tender changed after it was made.

**A refusal is visible.** The customer sees the remaining allowance before the click, a limit notice at the moment of the click
(`src/api/templates/_flash_limit.html`: "used N of M, the analysis was not started") and an upgrade action after it. No model is called and nothing is charged.

## 3. Fast verdicts

- **Relevance** (level `standard`): the AI worker (`src/workers/ai.py`) analyses a tender that matched a subscriber's template, before it is delivered. If the gate says no, the card is delivered without an analysis (`_degrade_to_plain`) instead of being dropped.
- **Bid verdict** (level `bid`, on a button): `go`, `caution` or `no_go` with reasons (`BidVerdictOutput` in `src/ai/schemas.py`).

These levels read the tender description capped at 50,000 characters (`cap_description` in `src/ai/prompts.py`), which keeps the cost per call small and predictable.

## 4. Step-by-step analysis of long documentation

The plain path shows the model only the first 50,000 characters. On 19 October 2026, 371 of 1,902 active tenders from the largest source (20%) reached that cap; the longest text was 502,000 characters. For the deep analysis and the document checklist, `src/ai/stepwise.py` reads the whole text in small steps:

1. **Scout** (`scout`, no model). Step-by-step mode starts when the text is longer than 40,000 characters or has more than 4 readable documents.
2. **Chunking** (`split_documents`, `plan_chunks`, `select_chunks`). Split by document; a large document is cut into chunks of at most 30,000 characters with 1,500 characters of overlap; small documents are packed together. At most **20 steps** per analysis; documents' heads go first, and anything left out is reported as partial coverage.
3. **Steps** (`run_stepwise`, cheap model tier). Each step sees its chunk and a short summary of what was already found (not the whole journal) and returns requirements as JSON: kind, text, quote.
4. **Critic** (`critic_filter`, no model). A requirement stays only if its quote is found **word for word** in its own document, after normalising case, whitespace, quotes and Serbian script (`src/domain/text_norm.py`). Short quotes (under 12 characters) prove nothing and are dropped. The number of dropped items is stored.
5. **Synthesis** (`run_stepwise`, the tier of the level). The model reads the checked journal plus the first 8,000 characters of the text, at most 45,000 characters in all. The `requirements` journal and the coverage numbers (`stepwise`: chunks done and total, dropped by the critic, unreadable documents) are written by the code, not by the model.

If the journal is empty, the analysis falls back to the plain path rather than paying the strong model to summarise nothing. If no step could be read, the call fails without calling the strong model.

**This mode is behind a feature flag.** `AI_STEPWISE` is `off` (default), `auto` (the Scout decides) or `on` (forced, for checks). On 20 October 2026 production runs with the flag off; the plain path is used.

## 5. The ledger after the call

- Each step is recorded in `ai_usage` right after it runs, with real tokens and cost and `calls=0`, so it does not consume the customer's allowance (`step_recorder`, `record_usage`). A step that failed after being paid is recorded too.
- The synthesis is recorded by `finalize_with_insert` with `calls=1`: **one analysis is one unit of the limit**, however many steps it took.
- The record commits the transaction, which would drop the row-level-security tenant setting; `step_recorder` sets it again after each commit.
- `src/workers/ai_spend_report.py` reads the ledger to report spend per level.

## What the customer sees

On the tender page (`src/api/templates/_ai_artifact.html`) the requirements appear in a collapsible block: the requirement, the document it came from and the exact quote. A partial analysis says "analysed N of M chunks". The Telegram bot shows the ten main requirements and links to the full list. Interface text is in Serbian, Russian and English.

## Tests

| Claim | Test |
|---|---|
| Scout thresholds, chunk sizes, step cap | `tests/ai/test_stepwise.py` |
| Critic keeps real quotes and drops invented ones | `tests/ai/test_stepwise.py` |
| Each step is recorded; synthesis counts one unit | `tests/ai/test_stepwise_render_usage.py` |
| Limit gate, trial demo, subscription status | `tests/ai/test_usage_limit_decision.py`, `tests/ai/test_usage_auto_limit_decision.py` |
| Failed paid steps are still recorded | `tests/ai/test_truncation_spend.py`, `tests/ai/test_stepwise.py` |
