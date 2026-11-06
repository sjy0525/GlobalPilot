"""Team and creator scope shared by Brief, workflow and memory entry points."""

from uuid import UUID

from sqlalchemy.orm import Session

from app.core.http_errors import AppHTTPException
from app.modules.brands.models import Brand
from app.modules.briefs.models import Brief
from app.modules.teams.authorization import require_active_team_member


def owned_team_briefs_query(db: Session, *, team_id: UUID, actor_user_id: UUID):
    require_active_team_member(db, team_id=team_id, user_id=actor_user_id)
    return db.query(Brief).join(Brand, Brand.id == Brief.brand_id).filter(
        Brief.team_id == team_id,
        Brief.user_id == actor_user_id,
        Brand.team_id == team_id,
    )


def require_owned_team_brief(
    db: Session, *, team_id: UUID, actor_user_id: UUID, brief_id: UUID,
) -> Brief:
    brief = (
        owned_team_briefs_query(db, team_id=team_id, actor_user_id=actor_user_id)
        .filter(Brief.id == brief_id).populate_existing().one_or_none()
    )
    if brief is None:
        raise AppHTTPException(404, "营销任务不存在", code="BRIEF_NOT_FOUND")
    return brief


def load_owned_team_brief_cursor(
    cursor, *, team_id: UUID, actor_user_id: UUID, brief_id: UUID,
):
    """Use the workflow/worker's own connection, without holding it during IO."""
    cursor.execute(
        """SELECT b.* FROM briefs AS b
           JOIN brands AS brand ON brand.id = b.brand_id AND brand.team_id = b.team_id
           JOIN team_members AS m ON m.team_id = b.team_id AND m.user_id = b.user_id
           JOIN users AS u ON u.id = b.user_id
           WHERE b.id = %s AND b.team_id = %s AND b.user_id = %s
             AND m.status = 'active' AND m.role IN ('admin', 'employee')
             AND u.is_active = TRUE""",
        (brief_id, team_id, actor_user_id),
    )
    row = cursor.fetchone()
    if row is None:
        raise AppHTTPException(404, "营销任务不存在", code="BRIEF_NOT_FOUND")
    return row
