"""Input contracts for targeted revisions of a platform's body copy.

RevisionTaskInput is an internal input assembled from an authorized source
snapshot. Schema validation does not establish ownership or version freshness.
"""

from datetime import datetime
from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class BlockLengthLimit(BaseModel):
    """单个可修改段落的长度上限，同一段落可以同时约束两种计数。"""

    model_config = ConfigDict(extra="forbid")

    block_id: UUID
    unit: Literal["unicode_chars", "whitespace_words"] = Field(
        description=(
            "unicode_chars 使用 Python len(text)，按 Unicode 码点计数，包含空格、"
            "标点和换行；whitespace_words 使用 len(text.split())，按空白分隔项计数，"
            "不执行中文分词，也不按标点、连字符或撇号拆词。"
        ),
    )
    maximum: int = Field(ge=1, strict=True, description="包含边界的长度上限")


class RevisionRequirements(BaseModel):
    """一次改稿的修改要求。must_keep 是需要原样保留的原文片段。"""

    model_config = ConfigDict(extra="forbid")

    goal: str = Field(min_length=1)
    allowed_block_ids: list[UUID] = Field(min_length=1)
    must_keep: list[str] = Field(default_factory=list)
    length_limits: list[BlockLengthLimit] = Field(default_factory=list)

    @field_validator("goal")
    @classmethod
    def validate_goal(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("修改目标不能只包含空白字符")
        return value

    @field_validator("allowed_block_ids")
    @classmethod
    def validate_block_ids(cls, value: list[UUID]) -> list[UUID]:
        if len(value) != len(set(value)):
            raise ValueError("允许修改的段落 ID 不能重复")
        return value

    @field_validator("must_keep")
    @classmethod
    def validate_must_keep(cls, value: list[str]) -> list[str]:
        if any(not item.strip() for item in value):
            raise ValueError("必须保留的内容不能包含空白项")
        return value

    @field_validator("length_limits")
    @classmethod
    def validate_length_limits(cls, value: list[BlockLengthLimit]) -> list[BlockLengthLimit]:
        targets = [(limit.block_id, limit.unit) for limit in value]
        if len(targets) != len(set(targets)):
            raise ValueError("同一段落、同一计数单位只能设置一个长度上限")
        return value


class DraftBlock(BaseModel):
    """原稿中的一个段落，保留原文空白和稳定的段落 ID。"""

    model_config = ConfigDict(extra="forbid")

    block_id: UUID
    text: str = Field(min_length=1)

    @field_validator("text")
    @classmethod
    def validate_text(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("段落正文不能只包含空白字符")
        return value


class DraftSnapshot(BaseModel):
    """某个平台正文 body 的一个文稿版本，blocks 按原文顺序排列。"""

    model_config = ConfigDict(extra="forbid")

    draft_id: UUID
    version: int = Field(ge=1, strict=True)
    source_brief_id: UUID
    source_checkpoint_id: str = Field(min_length=1, max_length=128)
    platform: str = Field(min_length=1, max_length=50)
    blocks: list[DraftBlock] = Field(min_length=1)

    @field_validator("source_checkpoint_id", "platform")
    @classmethod
    def validate_source_text(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("来源检查点和平台不能只包含空白字符")
        return value

    @field_validator("blocks")
    @classmethod
    def validate_blocks(cls, value: list[DraftBlock]) -> list[DraftBlock]:
        block_ids = [block.block_id for block in value]
        if len(block_ids) != len(set(block_ids)):
            raise ValueError("同一份文稿中的段落 ID 不能重复")
        return value


class RevisionTaskInput(BaseModel):
    """后端加载可信原稿后，组装并交给改稿流程的内部输入。"""

    model_config = ConfigDict(extra="forbid")

    draft: DraftSnapshot
    requirements: RevisionRequirements

    @model_validator(mode="after")
    def validate_task(self) -> Self:
        existing_ids = {block.block_id for block in self.draft.blocks}
        unknown_ids = [
            block_id
            for block_id in self.requirements.allowed_block_ids
            if block_id not in existing_ids
        ]
        if unknown_ids:
            raise ValueError(f"允许修改的段落不在原稿中：{unknown_ids}")

        allowed_ids = set(self.requirements.allowed_block_ids)
        for limit in self.requirements.length_limits:
            if limit.block_id not in allowed_ids:
                raise ValueError(f"长度上限只能设置在允许修改的段落上：{limit.block_id}")

        missing_texts = [
            text
            for text in self.requirements.must_keep
            if not any(text in block.text for block in self.draft.blocks)
        ]
        if missing_texts:
            raise ValueError(f"必须保留的内容不在原稿中：{missing_texts}")

        return self


class BlockPatch(BaseModel):
    """对一个段落的完整替换，expected_text 必须与原段落逐字一致。"""

    model_config = ConfigDict(extra="forbid")

    block_id: UUID
    expected_text: str = Field(min_length=1)
    replacement_text: str = Field(min_length=1)

    @field_validator("expected_text", "replacement_text")
    @classmethod
    def validate_text(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("补丁正文不能只包含空白字符")
        return value


class DraftPatch(BaseModel):
    """针对某份原稿版本的一组段落补丁。"""

    model_config = ConfigDict(extra="forbid")

    draft_id: UUID
    base_version: int = Field(ge=1, strict=True)
    patches: list[BlockPatch] = Field(min_length=1)

    @field_validator("patches")
    @classmethod
    def validate_patches(cls, value: list[BlockPatch]) -> list[BlockPatch]:
        block_ids = [patch.block_id for patch in value]
        if len(block_ids) != len(set(block_ids)):
            raise ValueError("同一份补丁不能重复修改一个段落")
        return value


class DraftCandidate(BaseModel):
    """修改后的独立候选，base_version 记录来源而非新的正式版本。"""

    model_config = ConfigDict(extra="forbid")

    candidate_id: UUID
    draft_id: UUID
    base_version: int = Field(ge=1, strict=True)
    blocks: list[DraftBlock] = Field(min_length=1)

    @field_validator("blocks")
    @classmethod
    def validate_blocks(cls, value: list[DraftBlock]) -> list[DraftBlock]:
        block_ids = [block.block_id for block in value]
        if len(block_ids) != len(set(block_ids)):
            raise ValueError("候选中的段落 ID 不能重复")
        return value


class DraftValidationResult(BaseModel):
    """由后端生成的程序检查结果，不代表语义审核或人工批准。"""

    model_config = ConfigDict(extra="forbid")

    passed: bool = Field(strict=True)
    issues: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_result(self) -> Self:
        if self.passed != (not self.issues):
            raise ValueError("检查状态与问题列表不一致")
        return self


ReviewText = Annotated[str, Field(min_length=1, pattern=r"\S")]


class RevisionReviewData(BaseModel):
    model_config = ConfigDict(extra="forbid")


class BrandEvidence(RevisionReviewData):
    """后端读取并保存的证据快照；结构校验不证明来源与访问权限。"""

    evidence_id: ReviewText
    document_id: UUID
    document_version: int = Field(ge=1, strict=True)
    source: ReviewText
    quote: ReviewText


class RevisionReviewContext(RevisionReviewData):
    """本次语义检查采用的品牌规则和经过授权读取的证据。"""

    rules_version: ReviewText
    brand_rules: list[ReviewText]
    evidence: list[BrandEvidence]

    @field_validator("evidence")
    @classmethod
    def validate_evidence_ids(
        cls,
        value: list[BrandEvidence],
    ) -> list[BrandEvidence]:
        evidence_ids = [item.evidence_id for item in value]
        if len(evidence_ids) != len(set(evidence_ids)):
            raise ValueError("证据 ID 不能重复")
        return value


class CriterionReview(RevisionReviewData):
    """模型对一项语义要求的判断。"""

    verdict: Literal["met", "unmet", "uncertain"]
    note: ReviewText


class BrandRuleReview(CriterionReview):
    rule_index: int = Field(ge=0, strict=True)


class EvidenceReference(RevisionReviewData):
    evidence_id: ReviewText
    quote: ReviewText


class FactReview(RevisionReviewData):
    claim: ReviewText
    verdict: Literal["supported", "unsupported", "contradicted", "uncertain"]
    references: list[EvidenceReference]
    note: ReviewText


class BlockSemanticReview(RevisionReviewData):
    block_id: UUID
    tone: CriterionReview
    facts: list[FactReview]


class ModelSemanticReview(RevisionReviewData):
    """模型报告；最终检查状态只能由后端生成。"""

    goal: CriterionReview
    rules: list[BrandRuleReview]
    blocks: list[BlockSemanticReview] = Field(min_length=1)
    uncertainties: list[ReviewText]


class SemanticValidationResult(RevisionReviewData):
    """绑定输入快照的检查结果，通过不代表人工批准或当前版本有效。"""

    status: Literal["passed", "needs_revision", "needs_clarification", "error"]
    candidate_id: UUID
    input_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    review_version: ReviewText
    issues: list[ReviewText]
    model_review: ModelSemanticReview | None = None

    @model_validator(mode="after")
    def validate_result(self) -> Self:
        if (self.status == "passed") != (not self.issues):
            raise ValueError("检查状态与问题列表不一致")
        if self.status == "passed" and self.model_review is None:
            raise ValueError("通过语义检查必须包含模型报告")
        return self


class RevisionRepairOutput(RevisionReviewData):
    """Model text only; scope, source version and patch metadata are server-owned."""

    blocks: list[DraftBlock] = Field(default_factory=list)
    clarification_questions: list[ReviewText] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_action(self) -> Self:
        if bool(self.blocks) == bool(self.clarification_questions):
            raise ValueError("必须返回完整改稿段落或待澄清问题，且两者不能同时返回")
        return self


class RevisionCreateRequest(RevisionReviewData):
    platform: ReviewText = Field(max_length=50)
    expected_draft_id: UUID
    expected_draft_version: int = Field(ge=1, strict=True)
    requirements: RevisionRequirements
    brand_rules: list[ReviewText] = Field(default_factory=list)
    evidence_document_ids: list[UUID] = Field(default_factory=list)
    max_revisions: int = Field(default=3, ge=0, le=3, strict=True)
    idempotency_key: ReviewText = Field(max_length=128)

    @field_validator("evidence_document_ids")
    @classmethod
    def validate_document_ids(cls, value: list[UUID]) -> list[UUID]:
        if len(value) != len(set(value)):
            raise ValueError("证据文档 ID 不能重复")
        return value


class RevisionCreateInput(RevisionCreateRequest):
    """Trusted route input; the brief ID is taken from the URL."""

    brief_id: UUID


class RevisionCommandInput(RevisionReviewData):
    expected_task_version: int = Field(ge=1, strict=True)
    expected_checkpoint_id: ReviewText | None = None
    idempotency_key: ReviewText = Field(max_length=128)


class RevisionClarificationInput(RevisionCommandInput):
    feedback: ReviewText
    goal: ReviewText | None = None
    must_keep: list[ReviewText] | None = None
    length_limits: list[BlockLengthLimit] | None = None
    evidence_document_ids: list[UUID] = Field(default_factory=list)

    @field_validator("evidence_document_ids")
    @classmethod
    def validate_document_ids(cls, value: list[UUID]) -> list[UUID]:
        if len(value) != len(set(value)):
            raise ValueError("证据文档 ID 不能重复")
        return value


class RevisionReviewInput(RevisionCommandInput):
    action: Literal["approved", "rejected"]
    feedback: ReviewText | None = None
    blocks: list[DraftBlock] | None = Field(default=None, min_length=1)

    @model_validator(mode="after")
    def validate_review(self) -> Self:
        if self.action == "rejected" and self.feedback is None:
            raise ValueError("拒绝改稿时必须提供反馈")
        if self.action == "rejected" and self.blocks is not None:
            raise ValueError("拒绝时不能同时采纳人工修改")
        return self


class RevisionTaskOutput(RevisionReviewData):
    task_id: UUID
    brief_id: UUID
    platform: str
    status: str
    version: int
    checkpoint_id: str | None = None
    draft: DraftSnapshot
    requirements: RevisionRequirements
    original_requirements: RevisionRequirements | None = None
    review_context: RevisionReviewContext | None = None
    candidate: DraftCandidate | None = None
    validation: SemanticValidationResult | None = None
    max_revisions: int = 3
    revision_attempts: int = 0
    stop_reason: str | None = None
    issues: list[str] = Field(default_factory=list)
    interrupt: dict | None = None
    history: list[dict] = Field(default_factory=list)
    adopted_version: int | None = None
    created_at: datetime
    updated_at: datetime
