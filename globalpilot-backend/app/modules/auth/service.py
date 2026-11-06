from google.auth.exceptions import GoogleAuthError, TransportError
from sqlalchemy import case, or_, update
from sqlalchemy.exc import IntegrityError

from app.modules.auth.google_auth import verify_google_id_token
from app.core.http_errors import AppHTTPException
from app.modules.auth.security import (
    REFRESH_TOKEN_TYPE,
    create_token_pair,
    create_access_token,
    decode_token,
    authentication_error,
    ensure_account_active,
    verify_password,
)
from app.modules.users.models import User
from app.modules.users.service import user_unique_constraint

def get_user_by_email(db, email: str):
    return db.query(User).filter(User.email == email.strip().lower()).first()

def login(db, email, password):
    user = get_user_by_email(db, email)
    if not user:
        raise authentication_error("No account found for this email", "INVALID_CREDENTIALS")
    if user.password_hash is None:
        raise authentication_error("this account does not have a password", "INVALID_CREDENTIALS")
    if not verify_password(password, user.password_hash):
        raise authentication_error("Incorrect password", "INVALID_CREDENTIALS")
    ensure_account_active(user)
    access_token, refresh_token = create_token_pair(user.id)
    return access_token, refresh_token, user

def get_user_by_google_sub(db, google_sub: str):
    return db.query(User).filter(User.google_sub == google_sub).first()

def _user_from_google_id_info(id_info: dict) -> dict:
    email = id_info["email"].strip().lower()
    name = id_info.get("name") or email.split("@")[0]
    return {
        "id": id_info["sub"],
        "email": email,
        "name": name[:50],
    }


def google_account_conflict() -> AppHTTPException:
    return AppHTTPException(
        status_code=409,
        detail="Email already linked to another account",
        code="GOOGLE_ACCOUNT_CONFLICT",
    )


def _commit_google_user(db, user: User, profile: dict, *, retry_link: bool = True, link: bool = False) -> User:
    try:
        if link:
            ensure_account_active(user)
            # A unique constraint cannot arbitrate two different subjects
            # overwriting the same user row. Claim an unlinked row atomically;
            # PostgreSQL rechecks this predicate after any row-lock wait.
            linked = db.execute(
                update(User).where(
                    User.id == user.id,
                    or_(User.google_sub.is_(None), User.google_sub == profile["id"]),
                ).values(
                    google_sub=profile["id"],
                    nickname=case(
                        (or_(User.nickname.is_(None), User.nickname == ""), profile["name"]),
                        else_=User.nickname,
                    ),
                ).returning(User.id).execution_options(synchronize_session=False)
            ).scalar_one_or_none()
            if linked is None:
                db.rollback()
                raise google_account_conflict()
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        if user_unique_constraint(exc) is None:
            raise

        # Another login may have created or linked this identity while this
        # request was committing. Read again after rollback, never reuse the
        # uncommitted object's state.
        existing = get_user_by_google_sub(db, profile["id"])
        if existing:
            return existing
        existing = get_user_by_email(db, profile["email"])
        if existing:
            if existing.google_sub == profile["id"]:
                return existing
            if existing.google_sub:
                raise google_account_conflict() from exc
            if retry_link:
                return _commit_google_user(db, existing, profile, retry_link=False, link=True)
        raise google_account_conflict() from exc
    db.refresh(user)
    return user

def get_or_create_google_user(db, id_info: dict) -> User:
    profile = _user_from_google_id_info(id_info)
    google_sub = profile["id"]
    email = profile["email"]
    nickname = profile["name"]

    user = get_user_by_google_sub(db, google_sub)
    if user:
        return user

    user = get_user_by_email(db, email)
    if user:
        if user.google_sub and user.google_sub != google_sub:
            raise google_account_conflict()
        return _commit_google_user(db, user, profile, link=True)

    user = User(
        email=email,
        password_hash=None,
        google_sub=google_sub,
        nickname=nickname,
    )
    db.add(user)
    return _commit_google_user(db, user, profile)

def login_with_google(db, id_token: str):
    if not id_token:
        raise AppHTTPException(status_code=400, detail="No ID token provided", code="BAD_REQUEST")
    try:
        id_info = verify_google_id_token(id_token)
    except TransportError:
        # Certificate fetch failures are upstream errors, not invalid credentials.
        raise
    except (ValueError, GoogleAuthError) as exc:
        raise authentication_error("Invalid ID token", "INVALID_GOOGLE_TOKEN") from exc

    user = get_or_create_google_user(db, id_info)
    ensure_account_active(user)
    access_token, refresh_token = create_token_pair(user.id)
    return access_token, refresh_token, user


def refresh_session(db, refresh_token: str):
    user_id = decode_token(refresh_token, REFRESH_TOKEN_TYPE)
    user = db.query(User).filter(User.id == user_id).first()
    if user is None:
        raise authentication_error("用户不存在")
    ensure_account_active(user)
    return create_access_token(user.id), user


