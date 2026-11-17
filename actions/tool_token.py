"""
The tool token: how a tool server learns WHO is calling.

The model must never choose its own identity, so no tool has a `user` argument. Instead the agent starts a tool server for ONE person and gives it
a short-lived signed token (valid 5 minutes by default). The server verifies it at startup and then, on EVERY call, checks again that the person is
still active, still in the same company and that their token version has not moved (a password change or a disabled account ends the session at
once). Roles and the company are read from the users table, not from the token.

A login token (indexing/auth.py) and a tool token use different audiences: one cannot be used as the other, so a leaked tool token cannot sign in
to the API and a login token cannot start a tool server.
"""
import time
import uuid

import jwt

from indexing.access import Principal
from indexing.users import get_user
from ingestion.config import settings

ALGORITHM = "HS256"
REQUIRED = ["exp", "iat", "sub", "iss", "aud", "jti"]


class ToolAuthError(Exception):
    """The tool token is missing, forged, expired or no longer valid (the message is for logs and the server's stderr)."""


def _secret() -> str:
    secret = settings.auth_secret
    if not isinstance(secret, str) or len(secret) < 32:
        raise ToolAuthError("AUTH_SECRET is missing or shorter than 32 characters")
    return secret


def issue_tool_token(principal: Principal, now: float | None = None) -> str:
    user = get_user(principal.user_id)
    if user is None or not user.is_active:
        raise ToolAuthError("unknown or disabled user")
    issued = int(time.time() if now is None else now)
    claims = {"sub": user.user_id, "tenant": user.tenant, "tv": user.token_version, "iat": issued, "exp": issued + int(settings.tool_token_minutes) * 60,
              "iss": settings.auth_issuer, "aud": settings.tool_audience, "jti": uuid.uuid4().hex}
    return jwt.encode(claims, _secret(), algorithm=ALGORITHM)


def verify_tool_token(token: str) -> tuple[Principal, dict]:
    """(who is calling, the claims). Raises ToolAuthError for anything that is not a good, current tool token for an active person."""
    if not isinstance(token, str) or not token or len(token) > 4096:
        raise ToolAuthError("no tool token")
    try:
        claims = jwt.decode(token, _secret(), algorithms=[ALGORITHM], audience=settings.tool_audience, issuer=settings.auth_issuer, options={"require": REQUIRED})
    except jwt.PyJWTError as e:
        raise ToolAuthError(f"tool token rejected: {type(e).__name__}") from e
    if not all(isinstance(claims.get(c), str) for c in ("sub", "tenant")) or not isinstance(claims.get("tv"), int) or isinstance(claims.get("tv"), bool):
        raise ToolAuthError("tool token rejected: claims have the wrong type")
    user = get_user(claims["sub"])
    if user is None or not user.is_active:
        raise ToolAuthError("tool token rejected: unknown or disabled user")
    if claims["tenant"] != user.tenant or claims["tv"] != user.token_version:
        raise ToolAuthError("tool token rejected: the person's company or sign-in state changed")
    return Principal(user.tenant, user.roles, user_id=user.user_id), claims
