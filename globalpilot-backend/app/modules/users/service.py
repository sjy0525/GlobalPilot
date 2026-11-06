from sqlalchemy.exc import IntegrityError

from app.core.http_errors import AppHTTPException
from app.modules.auth.security import hash_password
from app.modules.users.models import User


def user_unique_constraint(exc: IntegrityError) -> str | None:
    """Match only the PostgreSQL unique constraints used by user identities."""
    original = exc.orig
    sqlstate = getattr(original, "sqlstate", None) or getattr(original, "pgcode", None)
    constraint = getattr(getattr(original, "diag", None), "constraint_name", None)
    if sqlstate == "23505" and constraint in {"users_email_key", "users_google_sub_key"}:
        return constraint
    return None


def email_already_registered() -> AppHTTPException:
    return AppHTTPException(
        status_code=409,
        detail="Email already registered",
        code="EMAIL_ALREADY_REGISTERED",
    )


def register_user(db, email, password):
    normalized = email.strip().lower()
    existing = db.query(User).filter(User.email == normalized).first()
    if existing:
        raise email_already_registered()

    user = User(email=normalized, password_hash=hash_password(password))
    db.add(user)
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        if user_unique_constraint(exc) == "users_email_key":
            raise email_already_registered() from exc
        raise
    db.refresh(user)
    return user
