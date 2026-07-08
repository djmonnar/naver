import asyncio
import os
from datetime import timedelta
from urllib.parse import quote

from fastapi import Request
from fastapi.responses import JSONResponse, RedirectResponse, Response

from firebase_config import env_bool, get_admin_app, uses_firestore


SESSION_COOKIE_NAME = os.environ.get("SESSION_COOKIE_NAME", "firebase_session")
DEFAULT_USER_ID = os.environ.get("DEFAULT_USER_ID", "default")
PROTECTED_PATH_PREFIXES = (
    "/places",
    "/keywords",
    "/check",
    "/report",
    "/api/rankings",
)


def auth_enabled() -> bool:
    explicit = os.environ.get("FIREBASE_AUTH_ENABLED")
    if explicit is not None:
        return env_bool("FIREBASE_AUTH_ENABLED")
    return uses_firestore()


def local_user() -> dict[str, str]:
    return {"uid": DEFAULT_USER_ID, "email": "", "name": "Local"}


def path_requires_auth(path: str) -> bool:
    if not auth_enabled():
        return False
    return path == "/" or any(path.startswith(prefix) for prefix in PROTECTED_PATH_PREFIXES)


def get_request_user(request: Request) -> dict[str, str]:
    return getattr(request.state, "user", local_user())


def _firebase_auth():
    from firebase_admin import auth as firebase_auth

    return firebase_auth


def _user_from_decoded(decoded: dict) -> dict[str, str]:
    email = decoded.get("email") or ""
    return {
        "uid": decoded["uid"],
        "email": email,
        "name": decoded.get("name") or email or decoded["uid"],
        "photo_url": decoded.get("picture") or "",
    }


async def authenticate_bearer_request(request: Request) -> dict[str, str] | None:
    auth_header = request.headers.get("authorization", "")
    scheme, _, token = auth_header.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        return None

    try:
        decoded = await asyncio.to_thread(
            _firebase_auth().verify_id_token,
            token.strip(),
            app=get_admin_app(),
        )
    except Exception:
        return None

    return _user_from_decoded(decoded)


async def authenticate_request(request: Request) -> dict[str, str] | None:
    if not auth_enabled():
        return local_user()

    bearer_user = await authenticate_bearer_request(request)
    if bearer_user:
        return bearer_user

    session_cookie = request.cookies.get(SESSION_COOKIE_NAME)
    if not session_cookie:
        return None

    try:
        decoded = await asyncio.to_thread(
            _firebase_auth().verify_session_cookie,
            session_cookie,
            check_revoked=True,
            app=get_admin_app(),
        )
    except Exception:
        return None

    return _user_from_decoded(decoded)


def unauthenticated_response(request: Request):
    accepts_json = "application/json" in request.headers.get("accept", "")
    if request.url.path.startswith(("/api", "/check")) or accepts_json:
        return JSONResponse({"detail": "login_required"}, status_code=401)

    next_url = request.url.path
    if request.url.query:
        next_url = f"{next_url}?{request.url.query}"
    return RedirectResponse(f"/login?next={quote(next_url, safe='')}", status_code=303)


async def create_session_cookie(id_token: str):
    app = get_admin_app()
    firebase_auth = _firebase_auth()
    expires_in = timedelta(days=int(os.environ.get("SESSION_COOKIE_DAYS", "5")))
    decoded = await asyncio.to_thread(firebase_auth.verify_id_token, id_token, app=app)
    session_cookie = await asyncio.to_thread(
        firebase_auth.create_session_cookie,
        id_token,
        expires_in=expires_in,
        app=app,
    )
    return session_cookie, decoded, expires_in


def set_session_cookie(response: Response, request: Request, session_cookie: str, expires_in: timedelta) -> None:
    secure = env_bool("SESSION_COOKIE_SECURE", default=request.url.scheme == "https")
    response.set_cookie(
        SESSION_COOKIE_NAME,
        session_cookie,
        max_age=int(expires_in.total_seconds()),
        httponly=True,
        secure=secure,
        samesite="lax",
    )


def clear_session_cookie(response: Response, request: Request) -> None:
    secure = env_bool("SESSION_COOKIE_SECURE", default=request.url.scheme == "https")
    response.delete_cookie(
        SESSION_COOKIE_NAME,
        httponly=True,
        secure=secure,
        samesite="lax",
    )
