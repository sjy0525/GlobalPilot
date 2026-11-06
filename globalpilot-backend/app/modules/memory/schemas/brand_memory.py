#保存一条记忆需要哪些数据

from datetime import datetime,timezone
from typing import Literal
from uuid import UUID,uuid4
from pydantic import AwareDatetime,BaseModel,ConfigDict,Field

class MemoryScope(BaseModel):
    """""
        由后端确定的团队、用户和品牌范围
    """""
    team_id:UUID
    user_id:UUID
    brand_profile_id:UUID

class MemorySource(BaseModel):
    """""
        记忆对应的用户原话及来源任务
    """""
    model_config = ConfigDict(
        extra="forbid",
        str_strip_whitespace=True,
    )
    brief_id:UUID
    checkpoint_id:str
    quote:str

class BrandMemory(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        str_strip_whitespace=True,
    )
    id:UUID=Field(default_factory=uuid4)
    content:str=Field(min_length=1,max_length=2000)
    regions:str
    platforms:list[str]=Field(default_factory=list)
    sources: list[MemorySource] = Field(min_length=1)
    duration:Literal["long_term","task_only"]="long_term"
    status:Literal["active","inactive"]="active"
    version:int=Field(default=1,ge=1)
    create_at:AwareDatetime=Field(
        default_factory=lambda:datetime.now(timezone.utc)
    )
    update_at:AwareDatetime=Field(
        default_factory=lambda:datetime.now(timezone.utc)
    )
    invalidation_reason:str|None=Field(
      default=None,
      min_length=1,
      max_length=1000,
    )
    
class CreateMemoryArgs(BaseModel):
    """模型可以填写的工具参数"""
    model_config=ConfigDict(
      extra="forbid",
      str_strip_whitespace=True
    )
    content:str= Field(
        min_length=1,
        max_length=2000,
        description="提炼后的长期品牌偏好",
    )
    regions: str = Field(
        min_length=1,
        max_length=100,
        description="单个适用地区，例如 eu-na",
    )
    platforms: list[str] = Field(default_factory=list)
    
class SearchMemoryArgs(BaseModel):
    """搜索已有品牌偏好的参数。"""

    model_config = ConfigDict(
        extra="forbid",
        str_strip_whitespace=True,
    )

    query: str = Field(
        min_length=1,
        max_length=500,
        description="用于查找已有品牌偏好的检索词",
    )
    
class UpdateMemoryArgs(BaseModel):
    """更新已有品牌偏好"""
    model_config=ConfigDict(
        extra="forbid",
        str_strip_whitespace=True
    )
    
    memory_id:UUID=Field(description="搜索工具返回的记忆 ID")
    expected_version:int=Field(
        ge=1,
        description="搜索工具返回的当前版本",
    )
    content:str=Field(
        min_length=1,
        max_length=2000,
        description="更新后的完整偏好内容",
    )
    regions: str | None = Field(
        default=None,
        min_length=1,
        max_length=100,
        description="省略或传 null 时保留原地区",
    )
    platforms: list[str] | None = Field(
        default=None,
        description="省略或传 null 时保留原平台，空列表表示不限平台",
    )
    
class InvalidateMemoryArgs(BaseModel):
    """将已撤回或过时的长期偏好标记失效。"""

    model_config = ConfigDict(
        extra="forbid",
        str_strip_whitespace=True,
    )

    memory_id: UUID = Field(description="搜索工具返回的记忆 ID")
    expected_version: int = Field(
        ge=1,
        strict=True,
        description="搜索工具返回的当前版本",
    )
    reason: str = Field(
        min_length=1,
        max_length=1000,
        description="该长期偏好不再适用的原因",
    )
        
