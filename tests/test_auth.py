"""Phase 5 Stage 3 tests: users, password hashing, login, signed tokens, and the attacks on them."""
import base64
import json
import time

import jwt
import psycopg
import pytest
from fastapi.testclient import TestClient

from api.main import app
from indexing import auth, manage as manage_mod, users
from indexing.access import Principal
from indexing.auth import AuthConfigError, AuthError, LocalJWTVerifier, issue_token
from indexing.db import connect
from indexing.users import UserError, create_user, hash_password, verify_password
from ingestion.config import settings

SECRET = "test-secret-" + "x" * 40
GOOD = "correct horse battery"


@pytest.fixture(autouse=True)
def secret(monkeypatch):
    monkeypatch.setattr(settings, "auth_secret", SECRET)
    auth.set_verifier(None)
    yield
    auth.set_verifier(None)


def sign(claims, key=SECRET, alg="HS256"):
    return jwt.encode(claims, key, algorithm=alg)


def good_claims(user, **over):
    now = int(time.time())
    c = {"sub": user.user_id, "tenant": user.tenant, "tv": user.token_version, "iat": now, "exp": now + 600,
         "iss": settings.auth_issuer, "aud": settings.auth_audience, "jti": "abc"}
    c.update(over)
    return {k: v for k, v in c.items() if v is not None}


def verify(token):
    return LocalJWTVerifier().verify(token)


@pytest.fixture
def hr(db_url):
    return create_user("Priya", "acme", "hr,finance", GOOD)


# ---------------- passwords ----------------
def test_a_password_is_hashed_with_its_own_salt_and_never_stored_as_text():
    a, b = hash_password(GOOD), hash_password(GOOD)
    assert a != b and GOOD not in a and a.startswith("scrypt$")                  # same password, different salt, different hash
    assert verify_password(GOOD, a) and verify_password(GOOD, b)
    assert not verify_password(GOOD + "x", a) and not verify_password("", a) and not verify_password(GOOD.upper(), a)


def test_unicode_passwords_work():
    h = hash_password("pässwörd-密码-🔑-long")
    assert verify_password("pässwörd-密码-🔑-long", h) and not verify_password("passwörd-密码-🔑-long", h)


@pytest.mark.parametrize("stored", ["", "garbage", "scrypt$1$2", "bcrypt$16384$8$1$AAAA$AAAA", "scrypt$x$8$1$AAAA$AAAA", "scrypt$16384$8$1$!!!$!!!",
                                    "scrypt$999999999$8$1$AAAA$AAAA", "scrypt$16384$8$1$AAAA", None, 5])
def test_a_malformed_or_hostile_stored_hash_is_simply_a_no(stored):
    assert verify_password(GOOD, stored) is False                                 # never an exception, never "yes", never absurd work


@pytest.mark.parametrize("password", ["short", "x" * 129, "", None, 12345678901234])
def test_password_rules(password):
    with pytest.raises(UserError):
        users.check_password_rules(password)


def test_a_password_may_not_contain_the_username():
    with pytest.raises(UserError, match="username"):
        users.check_password_rules("my-priya-password", "priya")


# ---------------- the users table ----------------
def test_create_user_normalises_and_validates(db_url):
    u = create_user("  Rahul.K ", "acme", " HR , finance ", GOOD)
    assert (u.username, u.tenant, u.roles, u.is_active, u.token_version) == ("rahul.k", "acme", ("finance", "hr"), True, 0)
    assert create_user("plain-employee", "acme", "", GOOD).roles == ()                 # no special role is allowed


@pytest.mark.parametrize("args, message", [
    (("ab", "acme", "", GOOD), "username"), (("has space", "acme", "", GOOD), "username"), (("UPPER_é", "acme", "", GOOD), "username"),
    (("valid.name", "Acme", "", GOOD), "invalid tenant"), (("valid.name", "unassigned", "", GOOD), "invalid tenant"),
    (("valid.name", "acme", "boss", GOOD), "unknown role"), (("valid.name", "acme", "all", GOOD), "not a role a person can hold"),
    (("valid.name", "acme", "all,hr", GOOD), "cannot be combined|not a role"), (("valid.name", "acme", "", "short"), "characters"),
])
def test_bad_user_data_is_refused_and_nothing_is_created(db_url, args, message):
    with pytest.raises(UserError, match=message):
        create_user(*args)
    assert users.list_users() == []


def test_a_duplicate_username_is_refused_even_in_another_case(hr):
    with pytest.raises(UserError, match="already exists"):
        create_user("PRIYA", "globex", "", GOOD)


def test_the_database_itself_refuses_bad_users(db_url):
    for sql in ("('u1', 'ab', 'acme', '{}', 'h')", "('u2', 'okname', 'unassigned', '{}', 'h')", "('u3', 'okname', 'acme', '{all}', 'h')",
                "('u4', 'okname', 'Acme', '{}', 'h')", "('u5', 'Ok Name', 'acme', '{}', 'h')"):
        with pytest.raises(psycopg.errors.CheckViolation):
            with connect() as conn:
                conn.execute(f"INSERT INTO users (user_id, username, tenant_id, roles, password_hash) VALUES {sql}")


def test_the_stored_hash_is_not_the_password_and_listing_never_shows_it(hr):
    with connect() as conn:
        stored = conn.execute("SELECT password_hash FROM users").fetchone()[0]
    assert GOOD not in stored and verify_password(GOOD, stored)
    assert not hasattr(users.list_users()[0], "password_hash")


# ---------------- checking a login ----------------
def test_authenticate_accepts_the_right_password_and_nothing_else(hr):
    assert users.authenticate("priya", GOOD).user_id == hr.user_id
    assert users.authenticate("  PRIYA ", GOOD).user_id == hr.user_id                  # username is case-insensitive
    for username, password in (("priya", GOOD + "x"), ("priya", ""), ("nobody", GOOD), ("", GOOD), (None, GOOD), ("priya", None), ("priya", 5),
                               ("priya", "\ud800")):
        assert users.authenticate(username, password) is None


def test_authenticate_gives_the_same_answer_for_unknown_user_wrong_password_and_disabled(hr):
    users.set_active("priya", False)
    assert [users.authenticate("priya", GOOD), users.authenticate("priya", "wrong"), users.authenticate("ghost", GOOD)] == [None, None, None]


def test_an_unknown_username_still_costs_a_password_check(db_url, monkeypatch):
    """Otherwise a login form answers faster for unknown names and reveals which usernames exist."""
    calls = []
    real = users.verify_password
    monkeypatch.setattr(users, "verify_password", lambda p, s: calls.append(s) or real(p, s))
    assert users.authenticate("ghost", GOOD) is None
    assert len(calls) == 1 and calls[0].startswith("scrypt$")


def test_a_login_records_the_time(hr):
    with connect() as conn:
        assert conn.execute("SELECT last_login_at FROM users").fetchone()[0] is None
    users.authenticate("priya", GOOD)
    with connect() as conn:
        assert conn.execute("SELECT last_login_at FROM users").fetchone()[0] is not None


def test_changing_roles_password_or_state_updates_the_person(hr):
    assert users.set_roles("priya", "it").roles == ("it",)
    assert users.set_roles("priya", "").roles == ()
    with pytest.raises(UserError, match="not a role a person can hold"):
        users.set_roles("priya", "all")
    with pytest.raises(UserError, match="no user"):
        users.set_roles("ghost", "hr")
    v = users.get_user(hr.user_id).token_version
    assert users.set_password("priya", "a brand new password").token_version == v + 1
    assert users.authenticate("priya", GOOD) is None and users.authenticate("priya", "a brand new password")
    assert users.set_active("priya", False).token_version == v + 2 and users.set_active("priya", True).is_active


# ---------------- tokens ----------------
def test_a_login_gives_a_token_that_verifies_to_the_right_principal(hr):
    token, ttl, user = auth.login("priya", GOOD)
    assert ttl == settings.auth_token_minutes * 60
    p = verify(token)
    assert p == Principal("acme", ("finance", "hr"), user_id=hr.user_id)


def test_login_failures_all_look_the_same(hr):
    users.set_active("priya", False)
    messages = set()
    for u, p in (("priya", GOOD), ("priya", "x"), ("ghost", GOOD)):
        with pytest.raises(AuthError) as e:
            auth.login(u, p)
        messages.add(str(e.value))
    assert messages == {"invalid username or password"}


def test_a_token_contains_no_password_hash_and_no_roles(hr):
    token, _, _ = auth.login("priya", GOOD)
    payload = json.loads(base64.urlsafe_b64decode(token.split(".")[1] + "=="))
    assert set(payload) == {"sub", "tenant", "tv", "iat", "exp", "iss", "aud", "jti"}        # identity only: roles are read from the table


def test_tokens_are_short_lived_and_unique(hr):
    t1, ttl1, _ = auth.login("priya", GOOD)
    t2, _, _ = auth.login("priya", GOOD)
    assert t1 != t2 and jwt.decode(t1, options={"verify_signature": False})["exp"] - jwt.decode(t1, options={"verify_signature": False})["iat"] == ttl1 == 900


# ---------------- attacks ----------------
def test_a_token_signed_with_another_secret_is_rejected(hr):
    with pytest.raises(AuthError, match="InvalidSignatureError"):
        verify(sign(good_claims(hr), key="another-secret-" + "y" * 40))


def test_a_token_with_an_edited_payload_is_rejected(hr):
    """The attacker keeps the signature but changes who they are / which company / what they may do."""
    token, _, _ = auth.login("priya", GOOD)
    head, payload, sig = token.split(".")
    claims = json.loads(base64.urlsafe_b64decode(payload + "=="))
    for edit in ({"tenant": "globex"}, {"sub": "usr_someoneelse"}, {"roles": ["admin"]}, {"exp": claims["exp"] + 10**6}, {"tv": 99}):
        forged = base64.urlsafe_b64encode(json.dumps({**claims, **edit}).encode()).rstrip(b"=").decode()
        with pytest.raises(AuthError, match="InvalidSignatureError"):
            verify(f"{head}.{forged}.{sig}")


def test_the_none_algorithm_and_other_algorithms_are_refused(hr):
    claims = good_claims(hr)
    unsigned = jwt.encode(claims, key=None, algorithm="none")
    with pytest.raises(AuthError):
        verify(unsigned)
    with pytest.raises(AuthError):
        verify(unsigned.rsplit(".", 1)[0] + ".")
    with pytest.raises(AuthError):
        verify(sign(claims, alg="HS512"))                                       # even with the right secret: the algorithm is pinned


def test_an_expired_token_is_rejected(hr):
    old, _ = issue_token(hr, now=time.time() - 3600)
    with pytest.raises(AuthError, match="ExpiredSignatureError"):
        verify(old)


@pytest.mark.parametrize("claim", ["exp", "iat", "sub", "iss", "aud", "jti"])
def test_a_token_missing_a_required_claim_is_rejected(hr, claim):
    with pytest.raises(AuthError):
        verify(sign({k: v for k, v in good_claims(hr).items() if k != claim}))


@pytest.mark.parametrize("missing", ["tenant", "tv"])
def test_a_token_missing_tenant_or_version_is_rejected(hr, missing):
    with pytest.raises(AuthError):
        verify(sign({k: v for k, v in good_claims(hr).items() if k != missing}))


@pytest.mark.parametrize("over", [{"iss": "someone-else"}, {"aud": "another-api"}, {"tenant": 5}, {"tv": "0"}, {"tv": True}, {"sub": 7}, {"tenant": None, "tv": 0}])
def test_wrong_issuer_audience_or_claim_types_are_rejected(hr, over):
    with pytest.raises(AuthError):
        verify(sign(good_claims(hr, **over)))


def test_a_validly_signed_token_for_the_wrong_tenant_is_rejected(hr):
    """Signed by us, but claims a company the user does not belong to."""
    with pytest.raises(AuthError, match="tenant does not match"):
        verify(sign(good_claims(hr, tenant="globex")))


def test_roles_come_from_the_table_never_from_the_token(hr):
    claims = good_claims(hr, roles=["admin"], role="admin", is_admin=True)       # signed by us, with extra claims: they are ignored
    assert verify(sign(claims)).roles == ("finance", "hr")
    token, _, _ = auth.login("priya", GOOD)
    users.set_roles("priya", "admin")                                             # a change in the table applies to the very next request
    assert verify(token).roles == ("admin",)
    users.set_roles("priya", "")
    assert verify(token).roles == ()


def test_a_disabled_account_loses_access_immediately(hr):
    token, _, _ = auth.login("priya", GOOD)
    assert verify(token)
    users.set_active("priya", False)
    with pytest.raises(AuthError):
        verify(token)
    users.set_active("priya", True)
    with pytest.raises(AuthError, match="revoked"):                                 # re-enabling does not resurrect old tokens
        verify(token)


def test_a_disabled_flag_alone_is_enough_even_without_a_version_bump(hr):
    """Defence in depth: someone flips is_active directly in the database. The token must still stop working."""
    token, _, _ = auth.login("priya", GOOD)
    with connect() as conn:
        conn.execute("UPDATE users SET is_active = false")
    with pytest.raises(AuthError, match="unknown or disabled"):
        verify(token)


def test_a_password_change_revokes_every_earlier_token(hr):
    token, _, _ = auth.login("priya", GOOD)
    users.set_password("priya", "a brand new password")
    with pytest.raises(AuthError, match="revoked"):
        verify(token)
    assert verify(auth.login("priya", "a brand new password")[0]).user_id == hr.user_id


def test_a_token_for_a_deleted_user_is_rejected(hr):
    token, _, _ = auth.login("priya", GOOD)
    with connect() as conn:
        conn.execute("DELETE FROM users")
    with pytest.raises(AuthError, match="unknown or disabled"):
        verify(token)


@pytest.mark.parametrize("token", ["", None, "abc", "a.b.c", "....", "x" * 5000, 12345, b"bytes", ["list"], "Bearer abc"])
def test_garbage_is_rejected_cleanly(db_url, token):
    with pytest.raises(AuthError):
        verify(token)


# ---------------- configuration fails closed ----------------
@pytest.mark.parametrize("value", ["", "short", "x" * 31])
def test_without_a_usable_secret_no_token_is_issued_or_accepted(hr, monkeypatch, value):
    token, _, _ = auth.login("priya", GOOD)
    monkeypatch.setattr(settings, "auth_secret", value)
    with pytest.raises(AuthConfigError):
        issue_token(hr)
    with pytest.raises(AuthConfigError):
        verify(token)
    with pytest.raises(AuthConfigError):
        auth.login("priya", GOOD)


def test_the_verifier_can_be_swapped_and_an_unknown_provider_is_an_error(monkeypatch):
    class Company:
        def verify(self, token):
            return Principal("acme", ("hr",), user_id="from-their-idp")
    auth.set_verifier(Company())
    assert auth.get_verifier().verify("anything").user_id == "from-their-idp"
    auth.set_verifier(None)
    assert isinstance(auth.get_verifier(), LocalJWTVerifier)
    auth.set_verifier(None)
    monkeypatch.setattr(settings, "auth_provider", "okta")
    with pytest.raises(AuthConfigError, match="unknown auth provider"):
        auth.get_verifier()


# ---------------- the API ----------------
@pytest.fixture
def client(db_url):
    return TestClient(app, raise_server_exceptions=False)


def login_request(client, username, password):
    return client.post("/auth/login", json={"username": username, "password": password})


def test_the_login_endpoint_returns_a_token_that_works(hr, client):
    r = login_request(client, "priya", GOOD)
    assert r.status_code == 200 and r.json()["token_type"] == "bearer" and r.json()["expires_in"] == 900 and r.json()["request_id"]
    assert set(r.json()) == {"access_token", "token_type", "expires_in", "request_id"}
    assert verify(r.json()["access_token"]).user_id == hr.user_id


def test_every_failed_login_gets_the_same_401(hr, client):
    users.set_active("priya", False)
    create_user("rahul", "acme", "", GOOD)
    bodies = []
    for u, p in (("priya", GOOD), ("rahul", "wrong password"), ("ghost", GOOD)):
        r = login_request(client, u, p)
        assert r.status_code == 401 and r.headers["www-authenticate"] == "Bearer"
        body = r.json()
        body.pop("request_id")
        bodies.append(body)
    assert bodies == [{"detail": "Invalid username or password."}] * 3


def test_login_input_is_validated(client):
    assert client.post("/auth/login", json={"username": "", "password": "x"}).status_code == 422
    assert client.post("/auth/login", json={"username": "a", "password": ""}).status_code == 422
    assert client.post("/auth/login", json={"username": "a" * 65, "password": "x"}).status_code == 422
    assert client.post("/auth/login", json={"username": "a", "password": "x" * 201}).status_code == 422
    assert client.post("/auth/login", json={"username": "a"}).status_code == 422
    assert client.post("/auth/login", json={"username": ["a"], "password": "x"}).status_code == 422


def test_without_a_secret_login_is_a_503_that_reveals_nothing(hr, client, monkeypatch):
    monkeypatch.setattr(settings, "auth_secret", "")
    r = login_request(client, "priya", GOOD)
    assert r.status_code == 503 and "AUTH_SECRET" not in r.text and "secret" not in r.text.lower().replace("sign-in is not configured", "")


def test_passwords_usernames_and_tokens_never_reach_the_logs(hr, client):
    ok = login_request(client, "priya", GOOD).json()["access_token"]
    login_request(client, "priya", "my-wrong-password-attempt")
    login_request(client, "ghost-user", "another-secret-attempt")
    logs = settings.log_path.read_text(encoding="utf-8")
    assert "login_ok" in logs and "login_failed" in logs and hr.user_id in logs
    for forbidden in (GOOD, "my-wrong-password-attempt", "another-secret-attempt", "ghost-user", "priya", ok, SECRET):
        assert forbidden not in logs


def test_health_stays_public_and_ask_is_unchanged_until_stage_4(client):
    assert client.get("/health").status_code in (200, 503)                      # no token needed


# ---------------- the commands ----------------
def run(capsys, monkeypatch, *argv, password=GOOD):
    monkeypatch.setattr(manage_mod, "_ask_password", lambda: password)
    code = manage_mod.main(list(argv))
    return code, capsys.readouterr().out


def test_the_user_commands(db_url, capsys, monkeypatch):
    code, out = run(capsys, monkeypatch, "create-user", "Meera", "--tenant", "acme", "--roles", "hr,finance")
    assert code == 0 and "created meera" in out and "finance, hr" in out and GOOD not in out
    assert "already exists" in run(capsys, monkeypatch, "create-user", "meera", "--tenant", "acme")[1]
    run(capsys, monkeypatch, "create-user", "plain.user", "--tenant", "globex")
    code, out = run(capsys, monkeypatch, "users")
    assert code == 0 and "meera" in out and "finance, hr" in out and "(none)" in out and "2 user(s)" in out and "scrypt" not in out
    assert "roles are now it" in run(capsys, monkeypatch, "set-roles", "meera", "--roles", "it")[1]
    assert run(capsys, monkeypatch, "set-roles", "meera", "--roles", "boss")[0] == 1
    assert "DISABLED" in run(capsys, monkeypatch, "disable-user", "meera")[1] and "DISABLED" in run(capsys, monkeypatch, "users")[1]
    assert "enabled" in run(capsys, monkeypatch, "enable-user", "meera")[1]
    assert "password changed" in run(capsys, monkeypatch, "set-password", "meera", password="the newest password")[1]
    assert users.authenticate("meera", "the newest password")
    assert run(capsys, monkeypatch, "disable-user", "ghost")[0] == 1


def test_a_weak_password_or_a_typo_in_the_repeat_creates_nobody(db_url, capsys, monkeypatch):
    code, out = run(capsys, monkeypatch, "create-user", "meera", "--tenant", "acme", password="short")
    assert code == 1 and "characters" in out and users.list_users() == []

    def mismatch():
        raise UserError("the two passwords are not the same")
    monkeypatch.setattr(manage_mod, "_ask_password", mismatch)
    assert manage_mod.main(["create-user", "meera", "--tenant", "acme"]) == 1 and users.list_users() == []
    assert "not the same" in capsys.readouterr().out
