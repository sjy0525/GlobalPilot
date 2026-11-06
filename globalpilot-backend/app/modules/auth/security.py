# cspell:ignore pwdlib
from datetime import datetime, timedelta, timezone

import jwt
from fastapi import Depends
from fastapi.security import OAuth2PasswordBearer
from pwdlib import PasswordHash
from pwdlib.hashers.bcrypt import BcryptHasher
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.http_errors import AppHTTPException
from app.db.database import get_db
from app.modules.users.models import User

pwd_context = PasswordHash((BcryptHasher(),))
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/auth/login")

ACCESS_TOKEN_TYPE = "access"
REFRESH_TOKEN_TYPE = "refresh"


def authentication_error(detail: str, code: str = "INVALID_TOKEN") -> AppHTTPException:
    return AppHTTPException(
        status_code=401,
        detail=detail,
        code=code,
        headers={"WWW-Authenticate": "Bearer"},
    )


def ensure_account_active(user: User) -> None:
    if not user.is_active:
        raise AppHTTPException(
            status_code=403,
            detail="账号已停用",
            code="ACCOUNT_DISABLED",
            headers={"Cache-Control": "no-store", "Pragma": "no-cache"},
        )


def hash_password(password: str):
    return pwd_context.hash(password)

def verify_password(password: str, hashed: str):
    return pwd_context.verify(password, hashed)

def _encode_token(user_id: str, expire: datetime, token_type: str) -> str:
    payload = {
        "sub": str(user_id),
        "exp": expire,
        "token_type": token_type,
    }
    return jwt.encode(
        payload,
        settings.jwt_secret_key,
        algorithm=settings.jwt_algorithm,
    )

def create_access_token(user_id: str) -> str:
    expire = datetime.now(timezone.utc) + timedelta(
        minutes=settings.access_token_expire_minutes
    )
    return _encode_token(user_id, expire, ACCESS_TOKEN_TYPE)

def create_refresh_token(user_id: str) -> str:
    expire = datetime.now(timezone.utc) + timedelta(
        days=settings.refresh_token_expire_days
    )
    return _encode_token(user_id, expire, REFRESH_TOKEN_TYPE)

def create_token_pair(user_id: str) -> tuple[str, str]:
    return create_access_token(user_id), create_refresh_token(user_id)

def decode_token(token: str, expected_type: str) -> str:
    try:
        payload = jwt.decode(
            token,
            settings.jwt_secret_key,
            algorithms=[settings.jwt_algorithm],
        )
    except jwt.ExpiredSignatureError:
        raise authentication_error("token已过期", "TOKEN_EXPIRED")
    except jwt.PyJWTError:
        raise authentication_error("token无效")

    token_type = payload.get("token_type")
    # Older access tokens were issued without token_type.
    if expected_type == ACCESS_TOKEN_TYPE:
        if token_type not in (None, ACCESS_TOKEN_TYPE):
            raise authentication_error("token无效")
    elif token_type != expected_type:
        raise authentication_error("token无效")

    user_id = payload.get("sub")
    if not user_id:
        raise authentication_error("token无效")
    return str(user_id)

def get_current_user(
    token: str = Depends(oauth2_scheme),
    db: Session = Depends(get_db),
):
    user_id = decode_token(token, ACCESS_TOKEN_TYPE)
    user = db.query(User).filter(User.id == user_id).first()
    if user is None:
        raise authentication_error("用户不存在")
    ensure_account_active(user)
    return user
