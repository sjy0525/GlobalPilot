from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.http_errors import AppHTTPException, error_payload
from app.modules.auth.security import authentication_error, get_current_user
from app.db.database import get_db
from app.modules.users.models import User
from app.modules.auth.schemas import GoogleLoginRequest, LoginRequest, LoginResponse
from app.modules.users.schemas import UserResponse
from app.modules.auth.service import login as login_service
from app.modules.auth.service import login_with_google as google_login_service
from app.modules.auth.service import refresh_session as refresh_session_service

router = APIRouter()
REFRESH_COOKIE = "gp_refresh"
COOKIE_PATH = "/api/auth"


def trusted_browser_origin(request: Request) -> None:
    """Cookie endpoints need CSRF protection as well as CORS.

    Browsers send Origin on POST; non-browser clients may omit it. Never
    infer trust from forwarded Host headers. Use the configured allowlist.
    """
    origin = request.headers.get("origin")
    if origin:
        if origin not in settings.auth_allowed_origins:
            raise AppHTTPException(status_code=403, detail="不允许的请求来源", code="ORIGIN_FORBIDDEN")
    elif request.headers.get("sec-fetch-site") == "cross-site":
        raise AppHTTPException(status_code=403, detail="不允许的请求来源", code="ORIGIN_FORBIDDEN")


def private_response(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"


def set_refresh_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        key=REFRESH_COOKIE, value=token, httponly=True,
        secure=settings.refresh_cookie_secure, samesite="lax",
        path=COOKIE_PATH, max_age=settings.refresh_token_expire_days * 86400,
    )
    private_response(response)


def clear_refresh_cookie(response: Response) -> None:
    response.delete_cookie(
        key=REFRESH_COOKIE, path=COOKIE_PATH, httponly=True,
        secure=settings.refresh_cookie_secure, samesite="lax",
    )
    private_response(response)


def login_payload(access_token: str, user):
    return {
        "access_token": access_token,
        "token_type": "bearer",
        "user": {"id": str(user.id), "email": user.email, "name": user.nickname},
    }


@router.post("/auth/google-login", response_model=LoginResponse,
             dependencies=[Depends(trusted_browser_origin)])
def google_login(body: GoogleLoginRequest, response: Response, db: Session = Depends(get_db)):
    access_token, refresh_token, user = google_login_service(db, body.id_token)
    set_refresh_cookie(response, refresh_token)
    return login_payload(access_token, user)


@router.post("/auth/login", response_model=LoginResponse,
             dependencies=[Depends(trusted_browser_origin)])
def login(body: LoginRequest, response: Response, db: Session = Depends(get_db)):
    access_token, refresh_token, user = login_service(db, body.email, body.password)
    set_refresh_cookie(response, refresh_token)
    return login_payload(access_token, user)


@router.post("/auth/refresh", response_model=LoginResponse,
             dependencies=[Depends(trusted_browser_origin)])
def refresh(request: Request, response: Response, db: Session = Depends(get_db)):
    # Never accept refresh tokens in JSON, query parameters or Authorization.
    token = request.cookies.get(REFRESH_COOKIE)
    try:
        if not token:
            raise authentication_error("请重新登录", "AUTHENTICATION_REQUIRED")
        access_token, user = refresh_session_service(db, token)
    except HTTPException as exc:
        if exc.status_code != 401:
            raise
        failure = JSONResponse(status_code=401, content=error_payload(exc), headers=exc.headers)
        # Do not let a late failed refresh delete a cookie from a newer login.
        private_response(failure)
        return failure
    # Keep the refresh session's original expiry. No Set-Cookie here also means
    # an in-flight refresh cannot recreate the cookie after logout.
    private_response(response)
    return login_payload(access_token, user)


@router.post("/auth/logout", status_code=204,
             dependencies=[Depends(trusted_browser_origin)])
def logout():
    response = Response(status_code=204)
    clear_refresh_cookie(response)
    return response


@router.get("/auth/me", response_model=UserResponse)
def get_me(response: Response, current_user: User = Depends(get_current_user)):
    private_response(response)
    return current_user
