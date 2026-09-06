"""Test doubles. FakeModelClient satisfies aikit's ModelClient Protocol
(complete/stream/health/aclose, B.12 mirrors eval-platform's approach);
FakeEmbeddingService lets tests pin exact cosine similarity between known
strings instead of depending on a real, slow, network-bound model."""
from __future__ import annotations

import math
from collections.abc import AsyncIterator, Callable

from aikit.model_client import ChatMessage, Completion, StreamEvent, Usage


class FakeModelClient:
    def __init__(
        self,
        respond: Callable[[str], str] | None = None,
        *,
        name: str = "fake",
        fail: bool = False,
    ) -> None:
        self.name = name
        self._respond = respond or (lambda prompt: prompt)
        self._fail = fail
        self.calls: list[str] = []

    async def complete(
        self, messages: list[ChatMessage], *, model: str | None = None
    ) -> Completion:
        prompt = messages[-1].content
        self.calls.append(prompt)
        if self._fail:
            raise ConnectionError("backend unavailable")
        return Completion(
            content=self._respond(prompt), model=model or "fake-model",
            tokens_in=1, tokens_out=1, latency_ms=1.0,
        )

    async def stream(
        self, messages: list[ChatMessage], *, model: str | None = None
    ) -> AsyncIterator[StreamEvent]:
        prompt = messages[-1].content
        self.calls.append(prompt)
        if self._fail:
            raise ConnectionError("backend unavailable")
        text = self._respond(prompt)
        yield text
        yield Usage(tokens_in=1, tokens_out=1, finish_reason="stop")

    async def health(self) -> bool:
        return not self._fail

    async def aclose(self) -> None:
        pass


EMBEDDING_DIM = 384  # must match cache_entry.embedding's fixed vector(384) column


class FakeEmbeddingService:
    """Tests pin exact vectors per known input string so cosine similarity
    between any two texts is fully controlled, not model-dependent. Short
    vectors are zero-padded to EMBEDDING_DIM to satisfy the DB column's
    fixed dimension — padding with zeros never changes cosine similarity
    between two vectors padded the same way."""

    dim = EMBEDDING_DIM

    def __init__(self, vectors: dict[str, list[float]]) -> None:
        self._vectors = {
            text: vector + [0.0] * (EMBEDDING_DIM - len(vector))
            for text, vector in vectors.items()
        }

    async def embed(self, texts: list[str]) -> list[list[float]]:
        return [self._vectors[t] for t in texts]

    @staticmethod
    def cosine(a: list[float], b: list[float]) -> float:
        dot = sum(x * y for x, y in zip(a, b, strict=True))
        norm_a = math.sqrt(sum(x * x for x in a))
        norm_b = math.sqrt(sum(x * x for x in b))
        if norm_a == 0 or norm_b == 0:
            return 0.0
        return dot / (norm_a * norm_b)
