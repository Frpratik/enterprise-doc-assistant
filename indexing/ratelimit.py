"""
Rate limits and abuse guards (Phase 6, Stage 1). The counters live in Redis (shared by every API worker, survive a restart) and the
limiter FAILS CLOSED: if Redis cannot be reached, login and questions are refused with a 503, never allowed without limits.

What is limited, and why:
  login, per typed username   5 failures in 15 minutes, then 429 even with the right password (stops password guessing). The counter is
                              keyed on a keyed hash of the typed name whether or not the account exists, so a lockout reveals nothing
  login, per address          20 attempts in 15 minutes (stops one machine trying many names)
  questions, per address      120 a minute, checked BEFORE the token is verified (stops a flood of bad tokens from hammering the database)
  questions, per person       10 a minute and 150 a day: every question costs LLM quota (the main model allows ~300 answers a day for everyone)
  concurrent questions        2 at a time per person (a stream holds its slot until it ends or the person leaves)

Algorithm: a SLIDING WINDOW LOG (a sorted set of timestamps per key; Redis' own clock is used, so API servers with different clocks agree).
A fixed window lets a burst of 2x the limit through at the boundary; a sliding window does not.

Privacy: keys contain only keyed hashes of usernames and addresses, never the raw values.
"""
import hashlib
import hmac
import math
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass

from ingestion.config import settings


class LimiterUnavailable(Exception):
    """The counters cannot be reached. Callers must refuse the request (fail closed)."""


class RateLimited(Exception):
    def __init__(self, scope: str, retry_after: int):
        super().__init__(f"{scope}: retry in {retry_after}s")
        self.scope, self.retry_after = scope, max(1, int(retry_after))


@dataclass(frozen=True)
class Decision:
    allowed: bool
    count: int              # how many are in the window now (including this one when allowed)
    retry_after: int        # seconds until a slot frees up (0 when allowed)


# ---------------- the stores: the same contract, in memory (tests, one process) and in Redis ----------------
class MemoryStore:
    """Same behaviour as RedisStore, in one process. Used by the tests (with a controllable clock) and checked against Redis by the same contract tests."""
    def __init__(self, clock=time.time):
        self.clock, self.logs, self.slots = clock, {}, {}

    def _trim(self, key, window):
        now = self.clock()
        self.logs[key] = [t for t in self.logs.get(key, []) if t > now - window]
        return now, self.logs[key]

    def hit(self, key: str, limit: int, window: float) -> Decision:
        now, log = self._trim(key, window)
        if len(log) >= limit:
            return Decision(False, len(log), math.ceil(log[0] + window - now))
        log.append(now)
        return Decision(True, len(log), 0)

    def peek(self, key: str, limit: int, window: float) -> Decision:
        now, log = self._trim(key, window)
        if len(log) >= limit:
            return Decision(False, len(log), math.ceil(log[0] + window - now))
        return Decision(True, len(log), 0)

    def reset(self, key: str) -> None:
        self.logs.pop(key, None)

    def acquire(self, key: str, maximum: int, ttl: float) -> bool:
        now = self.clock()
        n, expires = self.slots.get(key, (0, 0))
        if expires <= now:
            n = 0
        if n >= maximum:
            return False
        self.slots[key] = (n + 1, now + ttl)
        return True

    def release(self, key: str) -> None:
        n, expires = self.slots.get(key, (0, 0))
        if n <= 1:
            self.slots.pop(key, None)
        else:
            self.slots[key] = (n - 1, expires)

    def ping(self) -> bool:
        return True


_HIT = """
local window = tonumber(ARGV[1]) * 1000
local limit = tonumber(ARGV[2])
local consume = ARGV[3] == '1'
local t = redis.call('TIME')
local now = t[1] * 1000 + math.floor(t[2] / 1000)
redis.call('ZREMRANGEBYSCORE', KEYS[1], 0, now - window)
local n = redis.call('ZCARD', KEYS[1])
if n >= limit then
  local oldest = redis.call('ZRANGE', KEYS[1], 0, 0, 'WITHSCORES')
  return {0, n, math.ceil((tonumber(oldest[2]) + window - now) / 1000)}
end
if consume then
  redis.call('ZADD', KEYS[1], now, ARGV[4])
  redis.call('PEXPIRE', KEYS[1], window)
  n = n + 1
end
return {1, n, 0}
"""
_ACQUIRE = """
local n = redis.call('INCR', KEYS[1])
redis.call('PEXPIRE', KEYS[1], tonumber(ARGV[2]) * 1000)
if n > tonumber(ARGV[1]) then
  redis.call('DECR', KEYS[1])
  return 0
end
return 1
"""
_RELEASE = """
local n = redis.call('DECR', KEYS[1])
if n <= 0 then redis.call('DEL', KEYS[1]) end
return n
"""


class RedisStore:
    def __init__(self, url: str | None = None):
        self.url, self._client, self._scripts = url, None, None

    def _redis(self):
        if self._client is None:
            import redis
            self._client = redis.Redis.from_url(self.url or settings.rate_redis_url, decode_responses=True, socket_timeout=1.0, socket_connect_timeout=1.0)
            self._scripts = {name: self._client.register_script(src) for name, src in (("hit", _HIT), ("acquire", _ACQUIRE), ("release", _RELEASE))}
        return self._client

    def _run(self, name, keys, args):
        import redis
        try:
            self._redis()
            return self._scripts[name](keys=keys, args=args)
        except redis.RedisError as e:
            raise LimiterUnavailable(f"{type(e).__name__}: {str(e)[:100]}") from e

    def hit(self, key, limit, window) -> Decision:
        ok, n, retry = self._run("hit", [key], [window, limit, "1", uuid.uuid4().hex])
        return Decision(bool(ok), int(n), int(retry))

    def peek(self, key, limit, window) -> Decision:
        ok, n, retry = self._run("hit", [key], [window, limit, "0", ""])
        return Decision(bool(ok), int(n), int(retry))

    def reset(self, key) -> None:
        import redis
        try:
            self._redis().delete(key)
        except redis.RedisError as e:
            raise LimiterUnavailable(f"{type(e).__name__}: {str(e)[:100]}") from e

    def acquire(self, key, maximum, ttl) -> bool:
        return bool(self._run("acquire", [key], [maximum, ttl]))

    def release(self, key) -> None:
        self._run("release", [key], [])

    def ping(self) -> bool:
        import redis
        try:
            return bool(self._redis().ping())
        except redis.RedisError:
            return False


# ---------------- the policies ----------------
class Limiter:
    def __init__(self, store):
        self.store = store

    # keys never contain a raw username or address
    @staticmethod
    def _k(kind: str, raw: str) -> str:
        secret = (settings.auth_secret or "no-secret").encode("utf-8")
        digest = hmac.new(secret, (raw or "").strip().lower().encode("utf-8", "replace"), hashlib.sha256).hexdigest()[:20]
        return f"{settings.rate_key_prefix}{kind}:{digest}"

    def _enforce(self, scope: str, d: Decision) -> None:
        if not d.allowed:
            raise RateLimited(scope, d.retry_after)

    # --- login ---
    def login_attempt(self, address: str) -> None:
        """Every login attempt from one address counts."""
        self._enforce("login_address", self.store.hit(self._k("login-ip", address), settings.rate_login_per_address, settings.rate_login_window_s))

    def login_account_open(self, username: str) -> None:
        """Raises RateLimited if too many failures were recorded for this typed username (does not count anything itself)."""
        self._enforce("login_account", self.store.peek(self._k("login-user", username), settings.rate_login_failures, settings.rate_login_window_s))

    def login_failed(self, username: str) -> None:
        self.store.hit(self._k("login-user", username), 10 ** 9, settings.rate_login_window_s)           # record only: the block decision is login_account_open

    def login_succeeded(self, username: str) -> None:
        self.store.reset(self._k("login-user", username))

    # --- questions ---
    def ask_address(self, address: str) -> None:
        self._enforce("ask_address", self.store.hit(self._k("ask-ip", address), settings.rate_ask_per_address_minute, 60))

    def ask_person(self, user_id: str) -> None:
        self._enforce("ask_minute", self.store.hit(self._k("ask-min", user_id), settings.rate_ask_per_minute, 60))
        d = self.store.hit(self._k("ask-day", user_id), settings.rate_ask_per_day, 86400)
        if not d.allowed:
            # the minute counter was already consumed: refund is not needed (the person is over budget anyway)
            raise RateLimited("ask_day", d.retry_after)

    def action_person(self, user_id: str) -> None:
        self._enforce("actions_minute", self.store.hit(self._k("act-min", user_id), settings.rate_actions_per_minute, 60))

    def acquire_slot(self, user_id: str) -> str:
        key = self._k("ask-slots", user_id)
        if not self.store.acquire(key, settings.rate_ask_concurrent, settings.rate_slot_ttl_s):
            raise RateLimited("ask_concurrent", 1)
        return key

    def release_slot(self, key: str) -> None:
        try:
            self.store.release(key)
        except LimiterUnavailable:
            pass                                                   # the slot expires by itself after rate_slot_ttl_s

    @contextmanager
    def slot(self, user_id: str):
        key = self.acquire_slot(user_id)
        try:
            yield
        finally:
            self.release_slot(key)

    def healthy(self) -> bool:
        return bool(self.store.ping())


_limiter: Limiter | None = None


def get_limiter() -> Limiter:
    global _limiter
    if _limiter is None:
        _limiter = Limiter(RedisStore())
    return _limiter


def set_limiter(limiter: Limiter | None) -> None:
    global _limiter
    _limiter = limiter


def client_address(request) -> str:
    """Who is calling. Behind a proxy the real address is in X-Forwarded-For, but ONLY a proxy we control may be believed (setting off by default)."""
    if settings.trust_proxy_headers:
        forwarded = request.headers.get("x-forwarded-for", "")
        if forwarded.strip():
            return forwarded.split(",")[0].strip()
    return (request.client.host if request.client else None) or "unknown"
