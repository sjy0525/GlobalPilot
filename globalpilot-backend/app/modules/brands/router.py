from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.database import get_async_db
from app.modules.brands.models import DEFAULT_BRAND_NAMES, Brand
from app.modules.teams.authorization import require_active_team_member_async
from app.modules.teams.dependencies import get_current_member_from_query
from app.modules.teams.models import TeamMember

router = APIRouter()


@router.get("/brands")
async def get_brands(
    member: TeamMember = Depends(get_current_member_from_query),
    db: AsyncSession = Depends(get_async_db),
):
    await require_active_team_member_async(db, team_id=member.team_id, user_id=member.user_id)
    result = await db.execute(select(Brand).where(
        Brand.team_id == member.team_id, Brand.name.in_(DEFAULT_BRAND_NAMES),
    ))
    rows = {
        brand.name: brand
        for brand in result.scalars().all()
    }
    return [
        {
            "id": str(rows[name].id),
            "team_id": str(rows[name].team_id),
            "name": name,
            "created_at": rows[name].created_at,
        }
        for name in DEFAULT_BRAND_NAMES
        if name in rows
    ]
