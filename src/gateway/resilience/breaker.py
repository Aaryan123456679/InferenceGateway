"""CircuitBreaker — closed -> open -> half-open -> closed, per backend,
Redis-backed. Every state transition happens inside a Lua script so N
concurrent gateway instances checking/updating the same backend's state
can't race each other into an inconsistent state (the moral equivalent of
eval-platform's CAS-guarded run finalization, B.5)."""
from __future__ import annotations

import time

from redis.asyncio import Redis

_ALLOW_SCRIPT = """
local key = KEYS[1]
local now = tonumber(ARGV[1])
local cooldown = tonumber(ARGV[2])

local state = redis.call('HGET', key, 'state')
if state == false or state == 'closed' then
    return 1
end
if state == 'open' then
    local opened_at = tonumber(redis.call('HGET', key, 'opened_at'))
    if now - opened_at >= cooldown then
        redis.call('HSET', key, 'state', 'half_open')
        return 1
    end
    return 0
end
-- half_open: allow the probe through
return 1
"""

_RECORD_SCRIPT = """
local key = KEYS[1]
local ok = ARGV[1]
local now = tonumber(ARGV[2])
local threshold = tonumber(ARGV[3])

local state = redis.call('HGET', key, 'state')
if state == false then state = 'closed' end

if ok == '1' then
    redis.call('HSET', key, 'state', 'closed', 'failures', 0)
else
    local failures = tonumber(redis.call('HGET', key, 'failures')) or 0
    failures = failures + 1
    if state == 'half_open' or failures >= threshold then
        redis.call('HSET', key, 'state', 'open', 'opened_at', now, 'failures', failures)
    else
        redis.call('HSET', key, 'failures', failures)
    end
end
return redis.call('HGET', key, 'state')
"""


def _key(backend_id: str) -> str:
    return f"breaker:{backend_id}"


class CircuitBreaker:
    def __init__(self, redis: Redis, *, failure_threshold: int, cooldown_seconds: int) -> None:
        self._redis = redis
        self._threshold = failure_threshold
        self._cooldown = cooldown_seconds
        self._allow_script = redis.register_script(_ALLOW_SCRIPT)
        self._record_script = redis.register_script(_RECORD_SCRIPT)

    async def allow(self, backend_id: str) -> bool:
        result = await self._allow_script(
            keys=[_key(backend_id)], args=[time.time(), self._cooldown]
        )
        return bool(int(result))

    async def record(self, backend_id: str, ok: bool) -> str:
        """Returns the resulting state (mostly useful for tests/metrics)."""
        state = await self._record_script(
            keys=[_key(backend_id)], args=["1" if ok else "0", time.time(), self._threshold]
        )
        return state.decode() if isinstance(state, bytes) else state
