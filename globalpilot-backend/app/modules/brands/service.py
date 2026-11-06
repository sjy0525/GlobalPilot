"""Provision each team's independent default product categories."""

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.modules.brands.models import DEFAULT_BRAND_NAMES, Brand
from app.modules.teams.models import Team


def seed_team_brands(db: Session, *, team_id: UUID) -> None:
    for name in DEFAULT_BRAND_NAMES:
        db.execute(
            insert(Brand).values(team_id=team_id, name=name)
            .on_conflict_do_nothing(index_elements=[Brand.team_id, Brand.name])
        )


def seed_all_team_brands(db: Session) -> None:
    for team_id in db.scalars(select(Team.id)):
        seed_team_brands(db, team_id=team_id)
