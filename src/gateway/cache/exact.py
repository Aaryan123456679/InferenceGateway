"""ExactCache — L1, Redis-backed exact prompt-hash lookup. Sub-millisecond;
tried before any embedding call."""
from __future__ import annotations

import json

from redis.asyncio import Redis

from gateway.cache import CacheHit
from gateway.domain import CacheTier


def _key(model: str, prompt_hash: str) -> str:
    return f"cache:{model}:{prompt_hash}"


class ExactCache:
    def __init__(self, redis: Redis, *, ttl_seconds: int) -> None:
        self._redis = redis
        self._ttl = ttl_seconds

    async def get(self, model: str, prompt_hash: str) -> CacheHit | None:
        raw = await self._redis.get(_key(model, prompt_hash))
        if raw is None:
            return None
        data = json.loads(raw)
        return CacheHit(
            content=data["content"],
            tokens_in=data["tokens_in"],
            tokens_out=data["tokens_out"],
            tier=CacheTier.L1,
        )

    async def put(
        self, model: str, prompt_hash: str, content: str, tokens_in: int, tokens_out: int
    ) -> None:
        payload = json.dumps(
            {"content": content, "tokens_in": tokens_in, "tokens_out": tokens_out}
        )
        await self._redis.set(_key(model, prompt_hash), payload, ex=self._ttl)
