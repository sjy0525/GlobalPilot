from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator
from typing import Literal
from datetime import datetime

from uuid import UUID

class TicketCreate(BaseModel):
    model_config=ConfigDict(
        extra='forbid',
        str_strip_whitespace=True
    )

    title:str=Field(
        min_length=1
    )

    description:str=''

    assignee_id:UUID

    reviewer_id:UUID

class TicketResultSubmit(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)

    processing_result: str = Field(min_length=1)


class TicketReview(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    decision: Literal["approve", "return"]
    expected_updated_at: AwareDatetime
    comment: str = ""

    @model_validator(mode="after")
    def check_return_comment(self) -> "TicketReview":
        if self.decision == "return" and not self.comment:
            raise ValueError("退回时必须填写审核意见")
        return self


class TicketReassign(BaseModel):
    model_config = ConfigDict(extra="forbid")

    assignee_id: UUID
    reviewer_id: UUID
    expected_updated_at: AwareDatetime


class TicketRead(BaseModel):
    model_config=ConfigDict(from_attributes=True)
    id:UUID
    team_id:UUID
    title:str
    description:str
    created_by:UUID
    assignee_id:UUID
    reviewer_id:UUID
    status:Literal['processing','awaiting_review','needs_info','completed']
    created_at:datetime
    updated_at:datetime
    processing_result: str | None = None
    review_comment: str | None = None


class TicketAssignmentLogRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    team_id: UUID
    ticket_id: UUID
    actor_user_id: UUID
    before_assignee_id: UUID
    after_assignee_id: UUID
    before_reviewer_id: UUID
    after_reviewer_id: UUID
    created_at: datetime

