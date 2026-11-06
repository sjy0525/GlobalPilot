from typing import Annotated

from pydantic import AfterValidator, BaseModel, Field
from pydantic_core import PydanticCustomError

PASSWORD_MAX_BYTES = 72


def validate_password_byte_length(password: str) -> str:
    try:
        encoded = password.encode("utf-8")
    except UnicodeEncodeError:
        raise PydanticCustomError(
            "password_invalid_encoding", "Password must contain valid UTF-8 text"
        ) from None
    if len(encoded) > PASSWORD_MAX_BYTES:
        raise PydanticCustomError(
            "password_too_long",
            "Password must not exceed {max_bytes} UTF-8 bytes",
            {"max_bytes": PASSWORD_MAX_BYTES},
        )
    return password


Password = Annotated[str, Field(min_length=6), AfterValidator(validate_password_byte_length)]

class LoginRequest(BaseModel):
    email: str
    password: Password

class GoogleLoginRequest(BaseModel):
    id_token: str

class AuthUser(BaseModel):
    id: str
    email: str
    name: str | None = None

class AccessTokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"

class LoginResponse(AccessTokenResponse):
    user: AuthUser
