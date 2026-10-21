import hashlib
import hmac
import secrets

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError
from fastapi import Depends, Request
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import ApiToken, User, utcnow

TOKEN_PREFIX = "chk_"

_hasher = PasswordHasher()


# --- passwords ---------------------------------------------------------------


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password_hash: str | None, password: str) -> bool:
    if not password_hash:
        return False
    try:
        return _hasher.verify(password_hash, password)
    except (VerifyMismatchError, InvalidHashError):
        return False


# --- API tokens --------------------------------------------------------------


def generate_token() -> str:
    return TOKEN_PREFIX + secrets.token_urlsafe(32)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def display_prefix(token: str) -> str:
    return token[: len(TOKEN_PREFIX) + 8]


class APIError(Exception):
    """An error rendered in OpenAI's `{"error": {...}}` shape."""

    def __init__(self, status: int, message: str, type_: str = "invalid_request_error", code: str | None = None):
        self.status = status
        self.message = message
        self.type = type_
        self.code = code

    def body(self) -> dict:
        return {"error": {"message": self.message, "type": self.type, "param": None, "code": self.code}}


def extract_token(request: Request) -> str | None:
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip() or None
    return request.headers.get("x-api-key") or None


def authenticate(db: Session, raw_token: str | None) -> ApiToken:
    if not raw_token:
        raise APIError(401, "Missing API key. Send 'Authorization: Bearer <token>'.", "authentication_error", "missing_api_key")
    token = db.query(ApiToken).filter(ApiToken.token_hash == hash_token(raw_token)).one_or_none()
    if token is None:
        raise APIError(401, "Invalid API key.", "authentication_error", "invalid_api_key")
    status = token.status()
    if status != "active":
        raise APIError(401, f"API key is {status}.", "authentication_error", f"api_key_{status}")
    if not token.user.is_active:
        raise APIError(403, "The user owning this API key is disabled.", "permission_error", "user_disabled")
    token.last_used_at = utcnow()
    db.commit()
    return token


# --- admin session -----------------------------------------------------------


class LoginRequired(Exception):
    pass


class CSRFError(Exception):
    pass


def require_admin(request: Request, db: Session = Depends(get_db)) -> User:
    admin_id = request.session.get("admin_id")
    if admin_id is None:
        raise LoginRequired()
    user = db.get(User, admin_id)
    if user is None or not user.is_admin or not user.is_active:
        request.session.clear()
        raise LoginRequired()
    return user


def csrf_token(request: Request) -> str:
    token = request.session.get("csrf")
    if not token:
        token = secrets.token_urlsafe(32)
        request.session["csrf"] = token
    return token


async def csrf_protect(request: Request) -> None:
    form = await request.form()
    sent = form.get("csrf_token")
    expected = request.session.get("csrf")
    if not expected or not isinstance(sent, str) or not hmac.compare_digest(sent, expected):
        raise CSRFError()
