from typing import TypedDict,Literal,Annotated
from uuid import UUID
import operator

class MarketingState(TypedDict,total=False):
    #表单字段
    brief_id:UUID
    team_id:UUID
    actor_user_id:UUID
    brand_id:UUID
    goal:str
    selling_points:str
    region:str
    user_persona:str
    social_media:list[str]
    purchase_channels:str|None
    competitor_url:str|None

    #工作流生成的数据
    context:str
    retrieved_context:str
    #自主调研产生的数据
    research:dict
    research_validation:dict
    strategy:str
    self_review:dict | None
    self_review_attempts:int
    content:dict
    # Server-owned link used to recover an adopted paragraph revision if the
    # parent checkpoint commits before its Brief projection is published.
    targeted_revision_task_id: str | None

    #人工审核节点及以后的数据
    decision:Literal["approved","rejected"]
    human_feedback:str|None
    revision_count:int
    final_output:str
    feedback_log:Annotated[list[str],operator.add]
    # Accepted human feedback is checkpointed with the decision. Publication
    # into the memory queue can therefore recover after an interrupted request.
    accepted_memory_events: Annotated[list[dict], operator.add]

#约束输入状态，用于验证输入数据
class MarketingInputState(TypedDict):
    brief_id: UUID
    team_id: UUID
    actor_user_id: UUID
    brand_id: UUID

    goal: str
    selling_points: str
    region: str
    user_persona: str

    social_media: list[str]

    purchase_channels: str | None
    competitor_url: str | None

#约束输出状态，用于验证输出数据
class MarketingOutputState(TypedDict):
    brief_id: UUID
    final_output: str

#生成策略节点需要的数据
class StrategyInput(MarketingInputState):
    context: str
    retrieved_context: str
    feedback_log: list[str]
    research:dict
    research_validation:dict

#生成图文的节点需要的状态
class ContentInput(MarketingInputState):
    strategy: str
    social_media: list[str]
    context: str
    retrieved_context: str
    feedback_log: list[str]
    self_review: dict | None
    research: dict
    research_validation: dict

#人工审核节点需要的状态
# LangGraph 会按这个 TypedDict 裁剪节点入参，revision_count 必须列出来，
# 否则每次拒绝都会按 0+1 写成 1，永远撞不上修改上限。
class HumanReviewInput(TypedDict):
    content: dict
    strategy: str
    revision_count: int
    self_review: dict | None
    self_review_attempts: int

#最终输出节点需要的状态
class FinalOutputInput(TypedDict):
    brief_id: UUID
    content: dict
    strategy: str
    self_review: dict | None
    self_review_attempts: int
