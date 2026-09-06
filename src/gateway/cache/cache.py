"""TwoTierCache — SRP: try L1, then L2, in that order. Knows nothing about
Redis or pgvector specifics; delegates entirely to ExactCache/SemanticCache."""
from __future__ import annotations

from gateway.cache import CacheHit
from gateway.cache.exact import ExactCache
from gateway.cache.semantic import SemanticCache


class TwoTierCache:
    def __init__(self, l1: ExactCache, l2: SemanticCache) -> None:
        self._l1 = l1
        self._l2 = l2

    async def get(self, *, model: str, prompt_hash: str, prompt_text: str) -> CacheHit | None:
        hit = await self._l1.get(model, prompt_hash)
        if hit is not None:
            return hit

        embedding = await self._l2.embed(prompt_text)
        if embedding is None:  # embeddings unavailable — L2 is disabled
            return None
        return await self._l2.get(model, embedding)

    async def put(
        self,
        *,
        model: str,
        prompt_hash: str,
        prompt_text: str,
        content: str,
        tokens_in: int,
        tokens_out: int,
    ) -> None:
        await self._l1.put(model, prompt_hash, content, tokens_in, tokens_out)

        embedding = await self._l2.embed(prompt_text)
        if embedding is None:
            return
        await self._l2.put(
            model=model,
            prompt_hash=prompt_hash,
            embedding=embedding,
            response=content,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
        )
