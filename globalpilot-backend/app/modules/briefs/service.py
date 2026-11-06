from fastapi import HTTPException
from sqlalchemy.orm import Session, load_only
from uuid import UUID

from app.modules.brands.models import Brand
from app.modules.briefs.models import Brief
from app.modules.users.models import User
from app.modules.briefs.schemas import BriefListItem, BriefRequest, BriefResponse
from app.modules.briefs.authorization import owned_team_briefs_query
from app.modules.teams.authorization import lock_team_and_require_member


def create_brief(
    db: Session, user: User, payload: BriefRequest, *, team_id: UUID,
) -> BriefResponse:
    lock_team_and_require_member(db, team_id=team_id, actor_user_id=user.id)
    brand = db.query(Brand).filter(
        Brand.id == payload.brand_id, Brand.team_id == team_id,
    ).first()
    if brand is None:
        raise HTTPException(status_code=404, detail="品牌不存在")

    brief = Brief(
        team_id=team_id,
        brand_id=brand.id,
        user_id=user.id,
        goal=payload.goal,
        selling_points=payload.selling_points,
        region=payload.region,
        user_persona=payload.user_persona,
        social_media=payload.social_media,
        purchase_channels=payload.purchase_channels,
        competitor_url=payload.competitor_url,
    )
    db.add(brief)
    db.flush()
    db.refresh(brief)
    return BriefResponse.model_validate(brief)


def list_briefs(db: Session, user: User, *, team_id: UUID) -> list[Brief]:
    return (
        owned_team_briefs_query(db, team_id=team_id, actor_user_id=user.id)
        .options(load_only(
            *(getattr(Brief, field) for field in BriefListItem.model_fields),
            raiseload=True,
        ))
        .filter(Brief.user_id == user.id, Brief.workflow_status.isnot(None))
        .order_by(Brief.updated_at.desc())
        .all()
    )
