from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator


class ResearchReport(BaseModel):
    status: Literal["not_needed", "completed", "insufficient"] = Field(
        description="无需外部调研、调研完成、资料不足"
    )

    summary: str = Field(
        min_length=1,
        max_length=2000,
        description="调研结论",
    )

    source_urls: list[str] = Field(
        default_factory=list,
        description="实际搜索得到且支持结论的来源链接",
    )

    uncertainties: list[str] = Field(
        default_factory=list,
        description="尚未证实或者缺少依据的信息",
    )

    @field_validator("summary", mode="before")
    @classmethod
    def strip_summary(cls, value: Any) -> Any:
        return value.strip() if isinstance(value, str) else value


class ResearchSentenceCheck(BaseModel):
    index: int = Field(ge=0)
    verdict: Literal["supported", "inference", "unsupported"]
    source: Literal["task", "web", "web_metadata", "process", "inference"]
    source_url: str | None = None
    quote: str = ""
    reason: str = ""


class ResearchAudit(BaseModel):
    task_answered: bool = Field(
        description="调研所需的信息问题是否已回答；不检查后续策略、文案或五部分交付",
    )
    checks: list[ResearchSentenceCheck]
    rejected_indices: list[int] = Field(
        default_factory=list,
        description="必须删除的句段index；优先于该句supported或inference判定，理由写在对应check.reason",
    )
    issues: list[str] = Field(
        default_factory=list,
        description="无法定位到具体句段的实质阻断；非空时整份摘要不可用。局部问题写入rejected_indices与check.reason",
    )
    limitations: list[str] = Field(
        default_factory=list,
        description="已声明且不妨碍回答本次调研问题的日期、样本或产品资料限制，不作为报告错误",
    )
