"""LatencyTracker — rolling p95 per backend from a capped recent-latency
list in Redis. Feeds LatencyAwarePolicy; a cold (empty) backend reports
`None` rather than 0, so new backends aren't penalized as "fastest"."""
from __future__ import annotations

from redis.asyncio import Redis


def _key(backend_id: str) -> str:
    return f"latency:{backend_id}"


class LatencyTracker:
    def __init__(self, redis: Redis, *, window_size: int = 200) -> None:
        self._redis = redis
        self._window_size = window_size

    async def record(self, backend_id: str, latency_ms: float) -> None:
        # redis-py's async stubs type these as `Awaitable[T] | T` (shared
        # with the sync client); redis.asyncio.Redis always returns the
        # awaitable at runtime.
        key = _key(backend_id)
        await self._redis.lpush(key, latency_ms)  # type: ignore[misc]
        await self._redis.ltrim(key, 0, self._window_size - 1)  # type: ignore[misc]

    async def p95(self, backend_id: str) -> float | None:
        raw = await self._redis.lrange(_key(backend_id), 0, -1)  # type: ignore[misc]
        if not raw:
            return None
        values = sorted(float(v) for v in raw)
        index = min(int(len(values) * 0.95), len(values) - 1)
        return values[index]
