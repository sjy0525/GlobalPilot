from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from app.modules.auth.schemas import Password

class UserCreate(BaseModel):
    email: str
    password: Password

class UserResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    email: str
    nickname: str | None = None
    created_at: datetime
    updated_at: datetime
