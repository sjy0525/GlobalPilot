from pydantic import BaseModel, ConfigDict, Field


COPY_VERSION = "globalpilot-copy-v2"


class CopyModel(BaseModel):
    model_config = ConfigDict(extra="ignore")


class PlatformCopy(CopyModel):
    hook: str = ""
    body: str = ""
    cta: str = ""
    tags: str = ""


class MarketingCopy(CopyModel):
    version: str = COPY_VERSION
    platforms: dict[str, PlatformCopy]


class GeneratedPlatformCopy(CopyModel):
    platform: str = Field(min_length=1)
    hook: str = ""
    body: str = ""
    cta: str = ""
    tags: str = ""


class GeneratedCopy(CopyModel):
    platforms: list[GeneratedPlatformCopy] = Field(default_factory=list)
