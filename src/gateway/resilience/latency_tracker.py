"""LatencyTracker — rolling p95 per backend from a capped recent-latency
list in Redis. Feeds LatencyAwarePolicy; a cold (empty) backend reports
`None` rather than 0, so new backends aren't penalized as "fastest"."""
from __future__ import annotations

from collections.abc import Awaitable
from typing import cast

from redis.asyncio import Redis


def _key(backend_id: str) -> str:
    return f"latency:{backend_id}"


class LatencyTracker:
    def __init__(self, redis: Redis, *, window_size: int = 200) -> None:
        self._redis = redis
        self._window_size = window_size

    async def record(self, backend_id: str, latency_ms: float) -> None:
        # redis-py's stubs type these as `Awaitable[T] | T` (shared with the
        # sync client) in some versions and plain `Awaitable[T]` in others;
        # redis.asyncio.Redis always returns the awaitable at runtime either
        # way. `cast` (unlike `# type: ignore`) doesn't care which stub
        # shape is actually installed, so it can't go stale across versions
        # the way a version-specific ignore comment did (caught by CI
        # resolving a different redis-py than this machine's).
        key = _key(backend_id)
        await cast(Awaitable[int], self._redis.lpush(key, latency_ms))
        await cast(Awaitable[int], self._redis.ltrim(key, 0, self._window_size - 1))

    async def p95(self, backend_id: str) -> float | None:
        raw = await cast(Awaitable[list[bytes]], self._redis.lrange(_key(backend_id), 0, -1))
        if not raw:
            return None
        values = sorted(float(v) for v in raw)
        index = min(int(len(values) * 0.95), len(values) - 1)
        return values[index]
