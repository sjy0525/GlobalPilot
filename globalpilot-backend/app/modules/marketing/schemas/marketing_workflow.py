from app.modules.marketing.schemas.content_review import ContentReview
from app.modules.marketing.schemas.copy import MarketingCopy
from app.modules.marketing.schemas.research_report import ResearchReport

from pydantic import BaseModel,Field,field_validator,model_validator
from uuid import UUID
from typing import Literal,Self


class MarketingWorkflowInput(BaseModel):
    brief_id: UUID

class MarketingWorkflowMiddleOutput(BaseModel):
    brief_id: UUID
    checkpoint_id: str | None = None
    status: Literal[
        "no_knowledge",
        "awaiting_review",
        "completed",
        "max_revisions",
        "self_review_failed",
    ]
    self_review: ContentReview | None = None
    research: ResearchReport | None = None
    research_validation: dict | None = None
    revision_count: int=0
    content: MarketingCopy | None = None
    strategy: str|None=None
    final_output: str|None=None


class MarketingWorkflowReviewInput(BaseModel):
    brief_id: UUID
    expected_checkpoint_id: str = Field(min_length=1, max_length=128)
    action: Literal["approved", "rejected"]
    feedback: str|None=None

    @field_validator("feedback")
    @classmethod
    def validate_feedback(cls,value:str|None)->str|None:
        if value is None:
            return None
        stripped=str(value).strip()
        return stripped or None

    @model_validator(mode="after")
    def require_feedback_when_rejected(self)->Self:
        if self.action=="rejected" and not self.feedback:
            raise ValueError("Feedback is required when rejecting a marketing workflow")
        return self
