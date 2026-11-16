"""
Tokens (Phase 5, Stage 3): a signed, short-lived proof of who someone is, and the swappable thing that checks it.

  login (username + password)  ->  issue_token(user)  ->  the client sends  Authorization: Bearer <token>
  every request                ->  verifier.verify(token)  ->  a Principal (company + roles)  ->  search

The rest of the system only knows the TokenVerifier interface: `verify(token) -> Principal`. Today that is LocalJWTVerifier (our own
HS256 tokens). A company that already has Okta / Azure AD / Keycloak would add a verifier that checks THEIR tokens and returns
the same Principal; nothing else changes. (Not built: no identity provider to test it against. The interface is the point.)

What a token proves, and what it does not:
  * It is signed with a secret only the server knows: a forged token or an edited claim (role, tenant, user) fails the signature.
  * The algorithm is pinned to HS256 ('none' and every other algorithm are refused), and exp / iat / sub / iss / aud / jti are required.
  * It expires quickly (default 15 minutes).
  * It says WHO. What the person may see is read from the users table on every request (so a role change, disabling an
    account or a new password takes effect immediately), and the tenant in the token must still match the table.
  * No secret configured (AUTH_SECRET in .env, 32+ characters) = no token is issued or accepted. Fail closed.
"""
import time
import uuid
from typing import Protocol

import jwt

from indexing.access import Principal
from indexing.users import User, get_user
from ingestion.config import settings

ALGORITHM = "HS256"
MIN_SECRET = 32
MAX_TOKEN_CHARS = 4096
REQUIRED = ["exp", "iat", "sub", "iss", "aud", "jti"]


class AuthError(Exception):
    """The token is missing, forged, expired or no longer valid. The message is for logs; users only learn 'invalid token'."""


class AuthConfigError(Exception):
    """The server is not set up for tokens (no usable secret). A 503, never a silent 'everything allowed'."""


class TokenVerifier(Protocol):
    def verify(self, token: str) -> Principal:
        """Return who is asking, or raise AuthError."""


def _secret() -> str:
    secret = settings.auth_secret
    if not isinstance(secret, str) or len(secret) < MIN_SECRET:
        raise AuthConfigError(f"AUTH_SECRET is missing or shorter than {MIN_SECRET} characters")
    return secret


def issue_token(user: User, now: float | None = None) -> tuple[str, int]:
    """(token, seconds until it expires). `now` exists for tests (a token issued in the past)."""
    issued = int(time.time() if now is None else now)
    ttl = int(settings.auth_token_minutes) * 60
    claims = {"sub": user.user_id, "tenant": user.tenant, "tv": user.token_version, "iat": issued, "exp": issued + ttl,
              "iss": settings.auth_issuer, "aud": settings.auth_audience, "jti": uuid.uuid4().hex}
    return jwt.encode(claims, _secret(), algorithm=ALGORITHM), ttl


class LocalJWTVerifier:
    def verify(self, token: str) -> Principal:
        if not isinstance(token, str) or not token or len(token) > MAX_TOKEN_CHARS:
            raise AuthError("no token or an unreasonable one")
        secret = _secret()
        try:
            claims = jwt.decode(token, secret, algorithms=[ALGORITHM], audience=settings.auth_audience, issuer=settings.auth_issuer,
                                options={"require": REQUIRED})
        except jwt.PyJWTError as e:
            raise AuthError(f"token rejected: {type(e).__name__}") from e
        if not all(isinstance(claims.get(c), str) for c in ("sub", "tenant")) or not isinstance(claims.get("tv"), int) or isinstance(claims.get("tv"), bool):
            raise AuthError("token rejected: claims have the wrong type")
        user = get_user(claims["sub"])
        if user is None or not user.is_active:
            raise AuthError("token rejected: unknown or disabled user")
        if claims["tenant"] != user.tenant:
            raise AuthError("token rejected: tenant does not match the user")
        if claims["tv"] != user.token_version:
            raise AuthError("token rejected: revoked (password changed or account disabled since it was issued)")
        return Principal(user.tenant, user.roles, user_id=user.user_id)        # roles and tenant from the TABLE, not from the token


_verifier: TokenVerifier | None = None


def get_verifier() -> TokenVerifier:
    """The verifier the API uses. One place to switch to a company's own identity provider."""
    global _verifier
    if _verifier is None:
        if settings.auth_provider != "local":
            raise AuthConfigError(f"unknown auth provider '{settings.auth_provider}' (only 'local' is built)")
        _verifier = LocalJWTVerifier()
    return _verifier


def set_verifier(verifier: TokenVerifier | None) -> None:
    """Install another verifier (or None to go back to the configured one)."""
    global _verifier
    _verifier = verifier


def login(username: str, password: str) -> tuple[str, int, User]:
    """Check the password and issue a token. Raises AuthError with ONE message for every kind of failure."""
    from indexing.users import authenticate
    _secret()                                                   # no secret: fail before touching the database
    user = authenticate(username, password)
    if user is None:
        raise AuthError("invalid username or password")
    token, ttl = issue_token(user)
    return token, ttl, user
