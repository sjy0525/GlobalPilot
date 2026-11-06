from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class ReviewModel(BaseModel):
    model_config = ConfigDict(extra="ignore")


class FactCheck(ReviewModel):
    claim: str = Field(min_length=1)
    claim_type: Literal["brand_product", "external_fact", "non_factual"] = Field(
        default="brand_product",
        description="本产品事实、外部市场事实或不含事实承诺的创意/任务受众/泛化CTA",
    )
    evidence_scope: Literal["brand", "research", "not_required"] = Field(
        default="brand",
        description="产品事实仅用品牌资料；外部事实用已通过核对的研究；非事实表达无需证据",
    )
    source_url: str | None = Field(default=None, description="外部事实对应的已核对研究来源链接")
    verdict: Literal["supported", "unsupported", "contradicted"]
    evidence: str  # Exact quote from the selected evidence scope; empty if unavailable.
    suggestion: str


class ModelReview(ReviewModel):
    facts: list[FactCheck]


class ContentReview(ReviewModel):
    facts: list[FactCheck]
    status: Literal["passed", "needs_revision", "error"]
    content_hash: str
    rules_version: str
    errors: list[str]
    blockers: list[str] = Field(default_factory=list)
    summary: str = ""
    attempts: int = 0
    stuck_at: str | None = None
