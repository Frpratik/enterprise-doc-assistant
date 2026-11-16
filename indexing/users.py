"""
People who can sign in (Phase 5, Stage 3): the users table, password hashing, and checking a login.

  * Passwords are hashed with scrypt (standard library) + a random salt per person. The hash string carries its own parameters,
    so they can be made stronger later and old hashes still verify.
  * Checking a password takes about the same time whether or not the username exists (a dummy hash is checked), and the caller gets
    the same answer for "no such user", "wrong password" and "account disabled": a login form must not reveal which usernames exist.
  * A person's company and roles live HERE. Tokens only say who someone is; what they may see is read from this table on every request.

Commands (python -m indexing.manage):  create-user, users, set-roles, set-password, disable-user, enable-user
"""
import base64
import hashlib
import hmac
import os
import re
import uuid
from dataclasses import dataclass

from indexing.access import ALL, AccessError, parse_roles, validate_tenant
from indexing.db import connect

_USERNAME = re.compile(r"^[a-z0-9][a-z0-9._-]{2,63}$")
MIN_PASSWORD, MAX_PASSWORD = 10, 128
_N, _R, _P = 2 ** 14, 8, 1                       # scrypt cost: ~16 MB and ~50 ms per check
_MAX_N = 2 ** 16                                 # never accept a stored hash that asks for absurd work


class UserError(ValueError):
    """Invalid user data (a username, a password, a role). The message is safe to show to an administrator."""


@dataclass(frozen=True)
class User:
    user_id: str
    username: str
    tenant: str
    roles: tuple[str, ...]
    is_active: bool
    token_version: int


# ---------------- passwords ----------------
def check_password_rules(password: str, username: str = "") -> None:
    if not isinstance(password, str) or not (MIN_PASSWORD <= len(password) <= MAX_PASSWORD):
        raise UserError(f"the password must be {MIN_PASSWORD} to {MAX_PASSWORD} characters")
    if username and username.lower() in password.lower():
        raise UserError("the password must not contain the username")


def hash_password(password: str) -> str:
    salt = os.urandom(16)
    digest = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=_N, r=_R, p=_P, dklen=32, maxmem=64 * 1024 * 1024)
    b64 = lambda b: base64.b64encode(b).decode("ascii")
    return f"scrypt${_N}${_R}${_P}${b64(salt)}${b64(digest)}"


def verify_password(password: str, stored: str) -> bool:
    """True only if `password` matches the stored hash. Never raises on a malformed hash or password: that is simply 'no'."""
    try:
        scheme, n, r, p, salt, digest = stored.split("$")
        n, r, p = int(n), int(r), int(p)
        if scheme != "scrypt" or n > _MAX_N or n < 2 or r > 16 or p > 4:
            return False
        expected = base64.b64decode(digest)
        got = hashlib.scrypt(password.encode("utf-8"), salt=base64.b64decode(salt), n=n, r=r, p=p, dklen=len(expected), maxmem=128 * 1024 * 1024)
        return hmac.compare_digest(got, expected)
    except (ValueError, TypeError, AttributeError):
        return False


_DUMMY = None


def _dummy_hash() -> str:
    global _DUMMY
    if _DUMMY is None:
        _DUMMY = hash_password("not-a-real-password-for-timing")
    return _DUMMY


# ---------------- the table ----------------
def _user(row) -> User:
    return User(row[0], row[1], row[2], tuple(row[3]), row[4], row[5])


COLUMNS = "user_id, username, tenant_id, roles, is_active, token_version"


def normalise_username(username: str) -> str:
    u = (username or "").strip().lower()
    if not _USERNAME.match(u):
        raise UserError("the username must be 3-64 characters: lowercase letters, digits, '.', '_' or '-', starting with a letter or digit")
    return u


def _roles(value) -> tuple[str, ...]:
    if isinstance(value, str) and not value.strip():
        return ()
    if not value:
        return ()
    try:
        roles = parse_roles(value)
    except AccessError as e:
        raise UserError(str(e)) from e
    if ALL in roles:
        raise UserError("'all' is a label for documents, not a role a person can hold")
    return roles


def create_user(username: str, tenant: str, roles, password: str) -> User:
    username = normalise_username(username)
    try:
        tenant = validate_tenant(tenant)
    except AccessError as e:
        raise UserError(str(e)) from e
    roles = _roles(roles)
    check_password_rules(password, username)
    user_id = "usr_" + uuid.uuid4().hex[:12]
    with connect() as conn:
        if conn.execute("SELECT 1 FROM users WHERE username = %s", (username,)).fetchone():
            raise UserError(f"the username '{username}' already exists")
        row = conn.execute(f"INSERT INTO users (user_id, username, tenant_id, roles, password_hash) VALUES (%s,%s,%s,%s,%s) RETURNING {COLUMNS}",
                           (user_id, username, tenant, list(roles), hash_password(password))).fetchone()
    return _user(row)


def get_user(user_id: str) -> User | None:
    with connect() as conn:
        row = conn.execute(f"SELECT {COLUMNS} FROM users WHERE user_id = %s", (user_id,)).fetchone()
    return _user(row) if row else None


def list_users() -> list[User]:
    with connect() as conn:
        return [_user(r) for r in conn.execute(f"SELECT {COLUMNS} FROM users ORDER BY tenant_id, username")]


def _update(username: str, sql: str, params: tuple) -> User:
    username = normalise_username(username)
    with connect() as conn:
        row = conn.execute(f"UPDATE users SET {sql} WHERE username = %s RETURNING {COLUMNS}", (*params, username)).fetchone()
    if not row:
        raise UserError(f"no user '{username}'")
    return _user(row)


def set_roles(username: str, roles) -> User:
    return _update(username, "roles = %s", (list(_roles(roles)),))


def set_active(username: str, active: bool) -> User:
    """Disabling also bumps token_version: tokens already issued stop working at once."""
    return _update(username, "is_active = %s, token_version = token_version + 1", (active,))


def set_password(username: str, password: str) -> User:
    """A new password also bumps token_version: every token issued with the old one stops working."""
    username = normalise_username(username)
    check_password_rules(password, username)
    return _update(username, "password_hash = %s, token_version = token_version + 1", (hash_password(password),))


def authenticate(username: str, password: str) -> User | None:
    """The user if the username and password are right and the account is active, else None (never says which part was wrong)."""
    try:
        username = (username or "").strip().lower()
        with connect() as conn:
            row = conn.execute(f"SELECT {COLUMNS}, password_hash FROM users WHERE username = %s", (username,)).fetchone()
            ok = verify_password(password if isinstance(password, str) else "", row[6] if row else _dummy_hash())
            if not (row and ok and row[4]):
                return None
            conn.execute("UPDATE users SET last_login_at = now() WHERE user_id = %s", (row[0],))
        return _user(row[:6])
    except (UnicodeEncodeError, ValueError):
        return None
