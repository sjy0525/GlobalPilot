from uuid import UUID

from pydantic import BaseModel, Field


class RagQuery(BaseModel):
    question: str = Field(min_length=1)
    brand_id: UUID
