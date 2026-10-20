"""Pydantic schemas of the JSON output of AI models (ARCHITECTURE §14.2 L1, §14.3 L2, §14.4 L3).

Purpose: validate the model's answer before it is written to ``tender_ai_analyses.output``.
The model may return extra/missing fields — the schemas are tolerant (``extra='ignore'``,
most fields Optional), but they structurally check the type of the key fields.

:func:`validate_output` — a single validation point by level (``quick`` → L1,
``standard`` → L2, ``deep`` → L3).
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class ClassificationItem(BaseModel):
    """One classifier code in the L1 output (``{system_code, code}``)."""

    model_config = ConfigDict(extra="ignore")

    system_code: str
    code: str


class L1Output(BaseModel):
    """Output of level 1 — classification (ARCHITECTURE §14.2).

    Does not depend on the subscriber; cached once per tender
    (``tender_ai_analyses`` with ``profile_id IS NULL``).
    """

    model_config = ConfigDict(extra="ignore")

    language: str | None = None
    predicted_classifications: list[ClassificationItem] = Field(default_factory=list)
    procedure_type: str | None = None
    is_subcontract: bool | None = None
    is_consulting_services: bool | None = None
    is_construction: bool | None = None
    topic_tags: list[str] = Field(default_factory=list)
    quality_score: float | None = None


class FocusAnswer(BaseModel):
    """Answer to one custom question of the subscriber (L2/L3)."""

    model_config = ConfigDict(extra="ignore")

    question: str
    answer: str | None = None


class L2Output(BaseModel):
    """Output of level 2 — standard analysis (ARCHITECTURE §14.3)."""

    model_config = ConfigDict(extra="ignore")

    summary: str | None = None
    match_score: int | None = None
    match_reasons: list[str] = Field(default_factory=list)
    red_flags: list[str] = Field(default_factory=list)
    required_qualifications: list[str] = Field(default_factory=list)
    estimated_effort_days: int | None = None
    competition_estimate: str | None = None
    focus_answers: list[FocusAnswer] = Field(default_factory=list)


class L3Output(BaseModel):
    """Output of level 3 — deep analysis on demand (ARCHITECTURE §14.4).

    Extends L2: adds ``work_breakdown`` (stages/subtasks) and
    ``participation_checklist`` (steps for submitting the bid). Cached as shared
    per tender (``profile_id IS NULL``), since at MVP extra_context/
    focus_questions are not mixed into the per-tender deep review.
    """

    model_config = ConfigDict(extra="ignore")

    summary: str | None = None
    match_score: int | None = None
    match_reasons: list[str] = Field(default_factory=list)
    red_flags: list[str] = Field(default_factory=list)
    required_qualifications: list[str] = Field(default_factory=list)
    estimated_effort_days: int | None = None
    competition_estimate: str | None = None
    work_breakdown: list[str] = Field(default_factory=list)
    participation_checklist: list[str] = Field(default_factory=list)
    focus_answers: list[FocusAnswer] = Field(default_factory=list)


class BidVerdictOutput(BaseModel):
    """Output of the bid/no-bid verdict — on demand per profile (VG-08).

    Cached per (tender_id, profile_id) with level='bid'.
    """

    model_config = ConfigDict(extra="ignore")

    verdict: str | None = None  # go | caution | no_go
    score: int | None = None  # 0–100
    pros: list[str] = Field(default_factory=list)
    cons: list[str] = Field(default_factory=list)


class ChecklistItem(BaseModel):
    """One document/proof in the participation checklist (VG-09)."""

    model_config = ConfigDict(extra="ignore")

    title: str
    category: str | None = None  # qualification | finance | equipment | legal | other
    mandatory: bool = True
    note: str = ""


class ChecklistDeadline(BaseModel):
    """A key deadline from the tender specification (VG-09)."""

    model_config = ConfigDict(extra="ignore")

    label: str
    value: str


class ChecklistOutput(BaseModel):
    """Output of the “what to prepare” checklist — on demand per profile (VG-09).

    Cached per (tender_id, profile_id) with level='checklist'.
    """

    model_config = ConfigDict(extra="ignore")

    documents: list[ChecklistItem] = Field(default_factory=list)
    deadlines: list[ChecklistDeadline] = Field(default_factory=list)
    disqualification_risks: list[str] = Field(default_factory=list)
    summary: str | None = None


class DraftSection(BaseModel):
    """One section of the bid draft (VG-10)."""

    model_config = ConfigDict(extra="ignore")

    heading: str
    body: str


class CriterionResponse(BaseModel):
    """Mapping to one evaluation criterion of the contracting authority (VG-10)."""

    model_config = ConfigDict(extra="ignore")

    criterion: str
    response: str


class DraftOutput(BaseModel):
    """Output of the AI bid draft — on demand per profile (VG-10).

    Cached per (tender_id, profile_id) with level='draft'.
    The body of the draft (sections.body, criteria_mapping.response) — in Serbian.
    The service summary/disclaimer — in the target_language of the profile.
    """

    model_config = ConfigDict(extra="ignore")

    sections: list[DraftSection] = Field(default_factory=list)
    criteria_mapping: list[CriterionResponse] = Field(default_factory=list)
    disclaimer: str | None = None
    summary: str | None = None


class TranslationOutput(BaseModel):
    """Output of the translation of a tender card sr→target_lang — on demand (VG-26).

    Cached in Redis by (tender_id, target_lang), NOT in ``tender_ai_analyses``:
    the translation is a display layer over the original, not an analysis artifact. The original in
    the DB stays untouched.

    The fields are NOT Optional (unlike the other schemas): showing a translation in which
    the model lost the title is worse than showing the original —
    a missing key must fail validation and fall back.
    """

    model_config = ConfigDict(extra="ignore")

    title: str
    description: str


# Analysis level (profiles.ai_analysis_level / tender_ai_analyses.level) → schema.
_SCHEMA_BY_LEVEL: dict[str, type[BaseModel]] = {
    "quick": L1Output,
    "standard": L2Output,
    "deep": L3Output,
    "bid": BidVerdictOutput,
    "checklist": ChecklistOutput,
    "draft": DraftOutput,
    "translate": TranslationOutput,
}


def validate_output(level: str, raw: dict[str, Any]) -> dict[str, Any]:
    """Validates the raw dict answer of the model by level, returns a normalized dict.

    Raises :class:`pydantic.ValidationError` if the structure is invalid, or
    :class:`KeyError` if the level is not supported.
    """
    schema = _SCHEMA_BY_LEVEL[level]
    return schema.model_validate(raw).model_dump()
