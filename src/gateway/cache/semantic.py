"""SemanticCache — L2, pgvector cosine-similarity lookup. Namespaced by
model: a near-duplicate prompt for model A never serves model B's cached
answer (B.7 correctness note).

`embeddings_factory` is called at most once, on first actual use, and
cached — loading a sentence-transformers model is slow and network-bound
and has no business gating gateway startup (same lesson as eval-platform's
lazy ScorerFactory, B.13 #5). A `None` return (embeddings unavailable)
disables L2 for the process lifetime; L1 is unaffected.
"""
from __future__ import annotations

from collections.abc import Callable

from aikit.db import session_scope
from aikit.embeddings import EmbeddingService
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from gateway.cache import CacheHit
from gateway.domain import CacheTier
from gateway.persistence.repositories import SemanticCacheRepo


class SemanticCache:
    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        embeddings_factory: Callable[[], EmbeddingService | None],
        *,
        similarity_threshold: float,
    ) -> None:
        self._sessionmaker = sessionmaker
        self._embeddings_factory = embeddings_factory
        self._embeddings: EmbeddingService | None = None
        self._embeddings_resolved = False
        self._threshold = similarity_threshold

    def _get_embeddings(self) -> EmbeddingService | None:
        if not self._embeddings_resolved:
            self._embeddings = self._embeddings_factory()
            self._embeddings_resolved = True
        return self._embeddings

    async def embed(self, text: str) -> list[float] | None:
        embeddings = self._get_embeddings()
        if embeddings is None:
            return None
        (vector,) = await embeddings.embed([text])
        return vector

    async def get(self, model: str, embedding: list[float]) -> CacheHit | None:
        async with session_scope(self._sessionmaker) as session:
            entry = await SemanticCacheRepo(session).find_similar(
                model=model, embedding=embedding, threshold=self._threshold
            )
        if entry is None:
            return None
        return CacheHit(
            content=entry.response,
            tokens_in=entry.tokens_in,
            tokens_out=entry.tokens_out,
            tier=CacheTier.L2,
        )

    async def put(
        self,
        *,
        model: str,
        prompt_hash: str,
        embedding: list[float],
        response: str,
        tokens_in: int,
        tokens_out: int,
    ) -> None:
        async with session_scope(self._sessionmaker) as session:
            await SemanticCacheRepo(session).insert(
                model=model,
                prompt_hash=prompt_hash,
                embedding=embedding,
                response=response,
                tokens_in=tokens_in,
                tokens_out=tokens_out,
            )
