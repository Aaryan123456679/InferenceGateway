"""RateLimiter — sliding-window-log over *tokens* (not request count), per
API key. `reserve` atomically trims the window, sums it, and adds a
provisional entry only if under budget; `reconcile` corrects that entry to
the true cost once the actual usage is known. Redis sorted-set + Lua so the
trim-sum-add sequence is one atomic operation across concurrent requests
for the same key."""
from __future__ import annotations

import time
import uuid

from redis.asyncio import Redis

_RESERVE_SCRIPT = """
local key = KEYS[1]
local now = tonumber(ARGV[1])
local window = tonumber(ARGV[2])
local budget = tonumber(ARGV[3])
local est_tokens = tonumber(ARGV[4])
local reservation_id = ARGV[5]

redis.call('ZREMRANGEBYSCORE', key, '-inf', now - window)

local members = redis.call('ZRANGE', key, 0, -1)
local total = 0
for _, m in ipairs(members) do
    local cost = string.match(m, ':(%-?%d+)$')
    total = total + tonumber(cost)
end

if total + est_tokens > budget then
    return 0
end

redis.call('ZADD', key, now, reservation_id .. ':' .. est_tokens)
redis.call('EXPIRE', key, window)
return 1
"""

_RECONCILE_SCRIPT = """
local key = KEYS[1]
local reservation_id = ARGV[1]
local actual_tokens = tonumber(ARGV[2])
local now = tonumber(ARGV[3])

local members = redis.call('ZRANGE', key, 0, -1)
for _, m in ipairs(members) do
    if string.sub(m, 1, string.len(reservation_id)) == reservation_id then
        redis.call('ZREM', key, m)
        redis.call('ZADD', key, now, reservation_id .. ':' .. actual_tokens)
        return 1
    end
end
return 0
"""


def _key(api_key_id: str) -> str:
    return f"ratelimit:{api_key_id}"


class RateLimiter:
    def __init__(self, redis: Redis) -> None:
        self._redis = redis
        self._reserve_script = redis.register_script(_RESERVE_SCRIPT)
        self._reconcile_script = redis.register_script(_RECONCILE_SCRIPT)

    async def reserve(
        self, api_key_id: str, est_tokens: int, *, budget: int, window_seconds: int
    ) -> str | None:
        """Returns a reservation id if under budget, else None (caller
        returns 429)."""
        reservation_id = str(uuid.uuid4())
        allowed = await self._reserve_script(
            keys=[_key(api_key_id)],
            args=[time.time(), window_seconds, budget, est_tokens, reservation_id],
        )
        return reservation_id if int(allowed) == 1 else None

    async def reconcile(self, api_key_id: str, reservation_id: str, actual_tokens: int) -> None:
        await self._reconcile_script(
            keys=[_key(api_key_id)], args=[reservation_id, actual_tokens, time.time()]
        )
