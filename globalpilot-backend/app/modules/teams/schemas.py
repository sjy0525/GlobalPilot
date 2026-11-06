from pydantic import BaseModel,ConfigDict
from typing import Literal
from uuid import UUID

#团队成员
class TeamMemberOption(BaseModel):
    user_id:UUID
    email:str
    nickname:str|None
    role:Literal['admin','employee']

#新增团队成员请求体
class TeamMemberCreate(BaseModel):
    model_config=ConfigDict(extra='forbid')
    email:str


class TeamMemberRoleUpdate(BaseModel):
    model_config = ConfigDict(extra='forbid')
    role: Literal['admin', 'employee']


class TeamMembershipOption(BaseModel):
    team_id: UUID
    team_name: str
    user_id: UUID
    role: Literal['admin', 'employee']
    status: Literal['active']
