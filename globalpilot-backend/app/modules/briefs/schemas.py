from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

Region = Literal["eu-na", "sea", "jp-kr", "other"]
SocialPlatform = Literal[
    "tiktok",
    "instagram",
    "youtube",
    "linkedin",
    "facebook",
    "x",
]


class BriefRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    brand_id: UUID
    goal: str = Field(min_length=1, max_length=1000)
    selling_points: str = Field(min_length=1, max_length=1000)
    region: Region
    user_persona: str = Field(min_length=1, max_length=255)
    social_media: list[SocialPlatform] = Field(min_length=1)
    purchase_channels: str | None = Field(default=None, max_length=255)
    competitor_url: str | None = Field(default=None, max_length=255)

    @field_validator("goal", "selling_points", "user_persona")
    @classmethod
    def strip_required(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("cannot be blank")
        return stripped

    @field_validator("social_media")
    @classmethod
    def unique_social_media(cls, value: list[SocialPlatform]) -> list[SocialPlatform]:
        unique: list[SocialPlatform] = []
        seen: set[SocialPlatform] = set()
        for platform in value:
            if platform not in seen:
                seen.add(platform)
                unique.append(platform)
        if not unique:
            raise ValueError("cannot be empty")
        return unique

    @field_validator("purchase_channels", "competitor_url", mode="before")
    @classmethod
    def empty_to_none(cls, value: str | None) -> str | None:
        if value is None:
            return None
        stripped = str(value).strip()
        return stripped or None


class BriefResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    team_id: UUID
    user_id: UUID
    brand_id: UUID
    created_at: datetime
    updated_at: datetime
    goal: str
    selling_points: str
    region: Region
    user_persona: str
    social_media: list[SocialPlatform]
    purchase_channels: str | None
    competitor_url: str | None


class BriefListItem(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    team_id: UUID
    user_id: UUID
    brand_id: UUID
    created_at: datetime
    updated_at: datetime
    goal: str
    selling_points: str
    region: Region
    user_persona: str
    social_media: list[SocialPlatform]
    purchase_channels: str | None
    competitor_url: str | None
    workflow_status: Literal[
        "no_knowledge",
        "awaiting_review",
        "completed",
        "max_revisions",
        "self_review_failed",
    ]
    revision_count: int
