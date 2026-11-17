"""Phase 6 Stage 1 tests: rate limits and abuse guards - the stores (memory and real Redis, same contract), the policies, and the API."""
import time
import uuid

import pytest
from fastapi.testclient import TestClient

from api.main import app
from indexing import audit, auth, ratelimit
from indexing.access import Principal
from indexing.db import connect
from indexing.ratelimit import Decision, Limiter, LimiterUnavailable, MemoryStore, RateLimited, RedisStore
from ingestion.config import settings

from test_api_access import PASSWORD, QUESTION, ask, bearer, token_of, world      # noqa: F401  (the 4-person, 4-document world and its helpers)

REDIS_URL = "redis://localhost:6379/15"


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


# ---------------- the stores: one contract, two implementations ----------------
def redis_up() -> bool:
    return RedisStore(REDIS_URL).ping()


@pytest.fixture(params=["memory", "redis"])
def store(request):
    """Returns (store, advance(seconds)). For Redis, time really passes (windows are 1 second)."""
    if request.param == "memory":
        clock = Clock()
        yield MemoryStore(clock), lambda s: setattr(clock, "t", clock.t + s)
        return
    if not redis_up():
        pytest.skip("no Redis at localhost:6379 (start Docker: docker compose up -d)")
    s = RedisStore(REDIS_URL)
    created = []
    real_hit, real_acq = s.hit, s.acquire
    s.hit = lambda key, *a: (created.append(key), real_hit(key, *a))[1]
    s.acquire = lambda key, *a: (created.append(key), real_acq(key, *a))[1]
    yield s, time.sleep
    for key in created:
        s.reset(key)


def key():
    return "t:" + uuid.uuid4().hex


def test_a_window_allows_up_to_the_limit_then_says_how_long_to_wait(store):
    s, _ = store
    k = key()
    for i in range(1, 4):
        assert s.hit(k, 3, 60) == Decision(True, i, 0)
    d = s.hit(k, 3, 60)
    assert d.allowed is False and d.count == 3 and 1 <= d.retry_after <= 60
    assert s.hit(k, 3, 60).allowed is False                       # a refused request is not counted: the window does not grow


def test_peek_never_consumes(store):
    s, _ = store
    k = key()
    for _ in range(5):
        assert s.peek(k, 2, 60) == Decision(True, 0, 0)
    s.hit(k, 2, 60), s.hit(k, 2, 60)
    d = s.peek(k, 2, 60)
    assert d.allowed is False and d.count == 2
    assert s.peek(k, 2, 60).count == 2


def test_the_window_slides_so_old_requests_stop_counting(store):
    s, advance = store
    k = key()
    s.hit(k, 2, 1), s.hit(k, 2, 1)
    assert s.hit(k, 2, 1).allowed is False
    advance(1.2)
    assert s.hit(k, 2, 1) == Decision(True, 1, 0)


def test_reset_and_key_isolation(store):
    s, _ = store
    a, b = key(), key()
    s.hit(a, 1, 60)
    assert s.hit(a, 1, 60).allowed is False and s.hit(b, 1, 60).allowed is True
    s.reset(a)
    assert s.hit(a, 1, 60).allowed is True


def test_slots_are_limited_released_and_expire_by_themselves(store):
    s, advance = store
    k = key()
    assert s.acquire(k, 2, 60) and s.acquire(k, 2, 60) and not s.acquire(k, 2, 60)
    s.release(k)
    assert s.acquire(k, 2, 60)
    s.release(k), s.release(k), s.release(k), s.release(k)         # releasing more than was taken is harmless
    assert s.acquire(k, 2, 60) and s.acquire(k, 2, 60) and not s.acquire(k, 2, 60)
    k2 = key()
    assert s.acquire(k2, 1, 1) and not s.acquire(k2, 1, 1)
    advance(1.3)
    assert s.acquire(k2, 1, 1)                                     # a slot a crashed request never released frees itself


def test_the_sliding_window_has_no_boundary_burst():
    """A fixed window would let 2x the limit through at the boundary; the sliding window does not (memory store: the clock is ours)."""
    clock = Clock()
    s = MemoryStore(clock)
    assert s.hit("k", 4, 10).allowed
    clock.t += 9.9
    assert all(s.hit("k", 4, 10).allowed for _ in range(3))        # 4 in the last 10 seconds
    clock.t += 0.2                                                 # the first one expired, the other 3 have not
    assert s.hit("k", 4, 10).allowed and not s.hit("k", 4, 10).allowed


# ---------------- the policies (memory store, our clock) ----------------
@pytest.fixture
def limiter(monkeypatch):
    clock = Clock()
    lim = Limiter(MemoryStore(clock))
    lim.clock = clock
    ratelimit.set_limiter(lim)
    for name, value in (("rate_login_failures", 5), ("rate_login_per_address", 20), ("rate_login_window_s", 900), ("rate_ask_per_address_minute", 120),
                        ("rate_ask_per_minute", 3), ("rate_ask_per_day", 5), ("rate_ask_concurrent", 2)):
        monkeypatch.setattr(settings, name, value)
    return lim


def test_five_failures_lock_the_name_for_the_whole_window_and_a_success_resets(limiter):
    for _ in range(4):
        limiter.login_account_open("Priya"), limiter.login_failed("Priya")
    limiter.login_succeeded("priya")                               # the person got it right: the counter starts over (and the name is case-insensitive)
    for _ in range(5):
        limiter.login_account_open("priya")
        limiter.login_failed("priya")
    with pytest.raises(RateLimited) as e:
        limiter.login_account_open("PRIYA")
    assert e.value.scope == "login_account" and 800 <= e.value.retry_after <= 900
    limiter.login_account_open("someone-else")                     # one name's lockout does not touch another
    limiter.clock.t += 901
    limiter.login_account_open("priya")                            # the lock ends by itself


def test_the_login_address_limit_counts_every_attempt(limiter, monkeypatch):
    monkeypatch.setattr(settings, "rate_login_per_address", 3)
    for _ in range(3):
        limiter.login_attempt("10.0.0.1")
    with pytest.raises(RateLimited, match="login_address"):
        limiter.login_attempt("10.0.0.1")
    limiter.login_attempt("10.0.0.2")


def test_a_person_has_a_minute_budget_and_a_day_budget(limiter):
    for _ in range(3):
        limiter.ask_person("u1")
    with pytest.raises(RateLimited) as e:
        limiter.ask_person("u1")
    assert e.value.scope == "ask_minute" and 1 <= e.value.retry_after <= 60
    limiter.ask_person("u2")                                       # another person is unaffected
    limiter.clock.t += 61
    limiter.ask_person("u1"), limiter.ask_person("u1")             # 5 in the day now
    limiter.clock.t += 61
    with pytest.raises(RateLimited) as e:
        limiter.ask_person("u1")
    assert e.value.scope == "ask_day" and e.value.retry_after > 3600
    limiter.clock.t += 86400
    limiter.ask_person("u1")


def test_slots_are_released_even_when_the_work_fails(limiter):
    with limiter.slot("u1"), limiter.slot("u1"):
        with pytest.raises(RateLimited, match="ask_concurrent"):
            limiter.acquire_slot("u1")
    assert limiter.store.slots == {}
    with pytest.raises(RuntimeError):
        with limiter.slot("u1"):
            raise RuntimeError("the question failed")
    assert limiter.store.slots == {}


def test_keys_never_contain_the_raw_username_or_address(limiter):
    limiter.login_attempt("203.0.113.7"), limiter.login_failed("Priya.Sharma"), limiter.ask_person("usr_abc123"), limiter.ask_address("203.0.113.7")
    keys = " ".join(limiter.store.logs)
    assert keys and not any(raw in keys for raw in ("203.0.113.7", "priya", "sharma", "usr_abc123")) and keys.count(settings.rate_key_prefix) == 5


def test_retry_after_is_at_least_one_second():
    assert RateLimited("x", 0).retry_after == 1 and RateLimited("x", 7.2).retry_after == 7


def test_the_client_address_ignores_forwarding_headers_unless_a_proxy_is_trusted(monkeypatch):
    class Req:
        client = type("C", (), {"host": "198.51.100.9"})()
        headers = {"x-forwarded-for": "203.0.113.5, 10.0.0.1"}
    assert ratelimit.client_address(Req) == "198.51.100.9"         # anyone can send that header: not believed by default
    monkeypatch.setattr(settings, "trust_proxy_headers", True)
    assert ratelimit.client_address(Req) == "203.0.113.5"

    class NoClient:
        client, headers = None, {}
    assert ratelimit.client_address(NoClient) == "unknown"


# ---------------- the API ----------------
def login(client, username, password):
    return client.post("/auth/login", json={"username": username, "password": password})


def body_of(r):
    b = r.json()
    b.pop("request_id", None)
    return {k: v for k, v in b.items() if k != "detail"} | {"detail": "".join(c for c in b["detail"] if not c.isdigit())}


def test_five_wrong_passwords_lock_the_account_even_for_the_right_password(world, limiter):
    client, _ = world
    assert [login(client, "anna", "wrong wrong wrong").status_code for _ in range(5)] == [401] * 5
    r = login(client, "anna", PASSWORD)                             # the RIGHT password, but the name is locked
    assert r.status_code == 429 and int(r.headers["retry-after"]) >= 800 and "access_token" not in r.text
    assert login(client, "hana", PASSWORD).status_code == 200      # someone else is not affected
    limiter.clock.t += 901
    assert login(client, "anna", PASSWORD).status_code == 200      # the lock ends by itself


def test_a_lockout_looks_the_same_for_a_name_that_does_not_exist(world, limiter):
    client, _ = world

    def sequence(name):
        return [(r.status_code, body_of(r)) for r in (login(client, name, "wrong wrong wrong") for _ in range(7))]
    assert sequence("anna") == sequence("nobody-by-this-name")      # nothing tells an attacker which usernames exist


def test_a_success_in_between_resets_the_failures(world, limiter):
    client, _ = world
    for _ in range(4):
        login(client, "anna", "wrong wrong wrong")
    assert login(client, "anna", PASSWORD).status_code == 200
    assert [login(client, "anna", "wrong wrong wrong").status_code for _ in range(4)] == [401] * 4


def test_one_address_cannot_try_many_names(world, limiter, monkeypatch):
    client, _ = world
    monkeypatch.setattr(settings, "rate_login_per_address", 4)
    assert [login(client, f"name{i}", "wrong wrong wrong").status_code for i in range(4)] == [401] * 4
    r = login(client, "hana", PASSWORD)                             # even a correct login from that address is refused now
    assert r.status_code == 429 and "access_token" not in r.text


def test_the_forwarding_header_cannot_be_used_to_dodge_the_address_limit(world, limiter, monkeypatch):
    client, _ = world
    monkeypatch.setattr(settings, "rate_login_per_address", 3)
    codes = [client.post("/auth/login", json={"username": f"n{i}", "password": "wrong wrong wrong"}, headers={"X-Forwarded-For": f"1.2.3.{i}"}).status_code
             for i in range(4)]
    assert codes == [401, 401, 401, 429]


def test_a_person_who_asks_too_much_gets_a_429_before_any_model_is_called(world, limiter):
    client, llm = world
    token = token_of(client, "anna")
    assert [ask(client, token).status_code for _ in range(3)] == [200] * 3 and len(llm.calls) == 3
    r = ask(client, token)
    assert r.status_code == 429 and 1 <= int(r.headers["retry-after"]) <= 60 and r.json()["request_id"] and len(llm.calls) == 3
    assert ask(client, token, path="/ask/stream").status_code == 429       # the same budget for the streaming endpoint
    assert ask(client, token_of(client, "hana")).status_code == 200        # another person is unaffected
    limiter.clock.t += 61
    assert ask(client, token).status_code == 200


def test_the_daily_budget_is_separate(world, limiter, monkeypatch):
    client, llm = world
    monkeypatch.setattr(settings, "rate_ask_per_minute", 100)
    monkeypatch.setattr(settings, "rate_ask_per_day", 2)
    token = token_of(client, "anna")
    assert [ask(client, token).status_code for _ in range(2)] == [200, 200]
    r = ask(client, token)
    assert r.status_code == 429 and int(r.headers["retry-after"]) > 3600 and len(llm.calls) == 2


def test_a_flood_of_bad_tokens_is_stopped_before_the_token_is_verified(world, limiter, monkeypatch):
    client, _ = world
    monkeypatch.setattr(settings, "rate_ask_per_address_minute", 3)
    verified = []
    real = auth.get_verifier()

    class Counting:
        def verify(self, token):
            verified.append(token)
            return real.verify(token)
    auth.set_verifier(Counting())
    codes = [client.post("/ask", json={"question": QUESTION}, headers=bearer("garbage")).status_code for _ in range(6)]
    assert codes == [401, 401, 401, 429, 429, 429] and len(verified) == 3          # the database is only touched for the first 3


def test_two_questions_at_a_time_per_person(world, limiter):
    client, llm = world
    token = token_of(client, "anna")
    uid = auth.get_verifier().verify(token).user_id
    first, second = limiter.acquire_slot(uid), limiter.acquire_slot(uid)            # two questions "in flight"
    for path in ("/ask", "/ask/stream"):
        r = ask(client, token, path=path)
        assert r.status_code == 429 and llm.calls == []
    limiter.release_slot(first)
    assert ask(client, token).status_code == 200
    limiter.release_slot(second)


@pytest.mark.parametrize("path", ["/ask", "/ask/stream"])
def test_a_question_gives_its_slot_back_when_it_ends_or_fails(world, limiter, monkeypatch, path):
    client, llm = world
    token = token_of(client, "anna")
    assert ask(client, token, path=path).status_code == 200 and limiter.store.slots == {}
    monkeypatch.setattr(llm, "chat", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("the model broke")))
    monkeypatch.setattr(llm, "stream", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("the model broke")))
    ask(client, token, path=path)
    assert limiter.store.slots == {}                                 # not a single leaked slot


def test_a_refusal_for_rate_is_a_log_line_not_a_database_row(world, limiter, monkeypatch):
    client, _ = world
    monkeypatch.setattr(settings, "rate_login_per_address", 2)
    def rows():
        with connect() as conn:
            return conn.execute("SELECT count(*) FROM audit_log").fetchone()[0]
    login(client, "x1", "wrong wrong wrong"), login(client, "x2", "wrong wrong wrong")
    before = rows()
    for _ in range(25):
        assert login(client, "x3", "wrong wrong wrong").status_code == 429
    assert rows() == before                                          # a flood of refusals must not become a flood of writes
    assert "rate_limited" in settings.log_path.read_text(encoding="utf-8")


def test_the_health_check_reports_the_limiter(world, limiter):
    client, _ = world
    assert client.get("/health").json()["rate_limiter"] is True


# ---------------- failing closed ----------------
@pytest.fixture
def dead_redis():
    ratelimit.set_limiter(Limiter(RedisStore("redis://localhost:1/0")))       # nothing listens there
    yield


def test_without_redis_login_and_questions_are_refused_not_unlimited(world, dead_redis):
    client, llm = world
    r = login(client, "anna", PASSWORD)
    assert r.status_code == 503 and r.headers["retry-after"] and "access_token" not in r.text
    ratelimit.set_limiter(Limiter(MemoryStore()))
    token = token_of(client, "anna")
    ratelimit.set_limiter(Limiter(RedisStore("redis://localhost:1/0")))
    for path in ("/ask", "/ask/stream"):
        r = ask(client, token, path=path)
        assert r.status_code == 503 and llm.calls == [] and "CANARY" not in r.text
    h = client.get("/health")
    assert h.json()["rate_limiter"] is False and h.json()["status"] == "degraded"


def test_a_store_that_fails_half_way_never_lets_the_request_through(world, limiter, monkeypatch):
    client, llm = world
    token = token_of(client, "anna")
    monkeypatch.setattr(limiter.store, "hit", lambda *a, **k: (_ for _ in ()).throw(LimiterUnavailable("boom")))
    assert ask(client, token).status_code == 503 and llm.calls == []
    assert login(client, "hana", PASSWORD).status_code == 503


# ---------------- request size ----------------
def test_a_big_body_is_refused_before_anything_reads_it(world, limiter, monkeypatch):
    client, llm = world
    monkeypatch.setattr(settings, "max_body_bytes", 300)
    token = token_of(client, "anna")
    r = client.post("/ask", content=b'{"question": "' + b"x" * 500 + b'"}', headers={**bearer(token), "Content-Type": "application/json"})
    assert r.status_code == 413 and r.headers["x-request-id"] and llm.calls == []
    assert login(client, "anna", "p" * 150).status_code == 401           # a normal request is fine
    assert client.get("/health").status_code in (200, 503)               # and GET has no body to check


def test_a_body_that_does_not_declare_its_size_is_refused(world, limiter):
    client, llm = world
    token = token_of(client, "anna")

    def chunks():
        yield b'{"question": "sick leaves?"}'
    r = client.post("/ask", content=chunks(), headers={**bearer(token), "Content-Type": "application/json"})
    assert r.status_code == 411 and llm.calls == []


# ---------------- the same behaviour on real Redis (the Lua scripts, the real clock) ----------------
@pytest.fixture
def real_redis(monkeypatch):
    if not redis_up():
        pytest.skip("no Redis at localhost:6379 (start Docker: docker compose up -d)")
    prefix = f"rl-test-{uuid.uuid4().hex[:8]}:"
    monkeypatch.setattr(settings, "rate_key_prefix", prefix)
    store = RedisStore(REDIS_URL)
    ratelimit.set_limiter(Limiter(store))
    yield store
    client = store._redis()
    for k in client.scan_iter(match=prefix + "*"):
        client.delete(k)


def test_the_whole_login_lockout_and_question_budget_work_on_real_redis(world, real_redis, monkeypatch):
    client, llm = world
    monkeypatch.setattr(settings, "rate_login_failures", 3)
    monkeypatch.setattr(settings, "rate_ask_per_minute", 2)
    assert [login(client, "anna", "wrong wrong wrong").status_code for _ in range(3)] == [401] * 3
    r = login(client, "anna", PASSWORD)
    assert r.status_code == 429 and int(r.headers["retry-after"]) >= 800
    token = token_of(client, "hana")
    assert [ask(client, token).status_code for _ in range(3)] == [200, 200, 429]
    assert real_redis._redis().keys(settings.rate_key_prefix + "*") and not any("anna" in k or "hana" in k for k in real_redis._redis().keys(settings.rate_key_prefix + "*"))
