from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


Verdict = Literal["pass", "fail", "pending"]


class ScoreCell(BaseModel):
    verdict: Verdict
    reason: str
    source: Literal["code", "model", "human"]


class EvalCaseOut(BaseModel):
    id: UUID
    code: str
    ordinal: int
    title: str
    user_request: str
    brand_profile: str
    brand_name: str
    platforms: list[str]
    must_include: list[str]
    forbidden_claims: list[str]
    region: str
    persona: str
    purchase_channels: str | None


class EvalTestSetOut(BaseModel):
    id: UUID
    version: str
    name: str
    description: str
    cases: list[EvalCaseOut]


class EvalRunOut(BaseModel):
    id: UUID
    team_id: UUID
    label: str
    status: str
    is_baseline: bool
    provenance: dict
    summary: dict | None
    report_markdown: str | None
    error: str | None
    started_at: datetime | None
    finished_at: datetime | None
    created_at: datetime
    task_total: int
    task_done: int


class EvalTaskOut(BaseModel):
    id: UUID
    case_id: UUID
    code: str
    title: str
    user_request: str
    brand_profile: str
    brand_name: str
    platforms: list[str]
    must_include: list[str]
    forbidden_claims: list[str]
    execution_status: str
    empty_output: bool
    timed_out: bool
    revision_count: int
    qualified: bool
    generated_content: dict | None
    strategy: str | None
    retrieved_context: str | None
    duration_ms: int | None
    api_cost_usd: float | None
    judge_cost_usd: float | None
    error: str | None
    scores: dict | None
    auto_scores: dict | None
    human_note: str | None
    failure_reasons: list[str]


class EvalRunCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    label: str | None = Field(default=None, max_length=200)
    workflow_version: str = Field(min_length=1, max_length=64)

    @field_validator("workflow_version")
    @classmethod
    def require_workflow_version(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("工作流版本不能为空")
        return stripped

    @model_validator(mode="after")
    def strip_label(self):
        if self.label is not None:
            self.label = self.label.strip() or None
        return self


class EvalReviewInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    facts: Verdict | None = None
    completeness: Verdict | None = None
    tone: Verdict | None = None
    format: Verdict | None = None
    note: str = Field(min_length=1, max_length=2000)

    @model_validator(mode="after")
    def require_change(self):
        self.note = self.note.strip()
        if not self.note:
            raise ValueError("纠正误判需要写下原因")
        if not any((self.facts, self.completeness, self.tone, self.format)):
            raise ValueError("至少修改一项判定")
        return self


class CompareSide(BaseModel):
    qualified: bool
    reasons: list[str]
    facts_verdict: str | None
    duration_ms: int | None
    api_cost_usd: float | None
    empty_output: bool
    timed_out: bool
    execution_status: str


class CompareRow(BaseModel):
    code: str
    title: str
    left: CompareSide
    right: CompareSide


class EvalCompareOut(BaseModel):
    left: EvalRunOut
    right: EvalRunOut
    rows: list[CompareRow]
    comparison_markdown: str
