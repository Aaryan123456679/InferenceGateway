from __future__ import annotations

from gateway.cache.cache import TwoTierCache
from gateway.cache.exact import ExactCache
from gateway.cache.semantic import SemanticCache
from gateway.domain import CacheTier
from tests.fakes import FakeEmbeddingService


def _make_cache(sessionmaker, redis, vectors: dict[str, list[float]], *, threshold: float = 0.9):
    embeddings = FakeEmbeddingService(vectors)
    l1 = ExactCache(redis, ttl_seconds=60)
    l2 = SemanticCache(sessionmaker, lambda: embeddings, similarity_threshold=threshold)
    return TwoTierCache(l1, l2)


async def test_l1_exact_hit_short_circuits_before_any_embedding(sessionmaker, redis):
    # put() always populates both tiers (so future paraphrases can L2-hit),
    # so "hello" needs a vector here; get()'s L1 check is what short-circuits
    # and must NOT need one — this test targets that get()-side property.
    cache = _make_cache(sessionmaker, redis, vectors={"hello": [1.0, 0.0, 0.0]})
    await cache.put(
        model="m", prompt_hash="h1", prompt_text="hello",
        content="answer", tokens_in=3, tokens_out=5,
    )
    hit = await cache.get(model="m", prompt_hash="h1", prompt_text="hello")
    assert hit is not None
    assert hit.tier == CacheTier.L1
    assert hit.content == "answer"
    assert (hit.tokens_in, hit.tokens_out) == (3, 5)


async def test_l2_hit_on_near_duplicate_prompt(sessionmaker, redis):
    vectors = {"original query": [1.0, 0.0, 0.0], "similar paraphrase": [0.99, 0.01, 0.0]}
    cache = _make_cache(sessionmaker, redis, vectors, threshold=0.9)
    await cache.put(
        model="m", prompt_hash="h-original", prompt_text="original query",
        content="answer", tokens_in=1, tokens_out=1,
    )
    hit = await cache.get(model="m", prompt_hash="h-different", prompt_text="similar paraphrase")
    assert hit is not None
    assert hit.tier == CacheTier.L2
    assert hit.content == "answer"


async def test_l2_respects_similarity_threshold(sessionmaker, redis):
    vectors = {"q1": [1.0, 0.0], "q2": [0.8, 0.6]}  # cosine(q1, q2) = 0.8
    cache = _make_cache(sessionmaker, redis, vectors, threshold=0.9)
    await cache.put(
        model="m", prompt_hash="h1", prompt_text="q1", content="answer", tokens_in=1, tokens_out=1
    )
    hit = await cache.get(model="m", prompt_hash="h2", prompt_text="q2")
    assert hit is None  # 0.8 < 0.9 threshold


async def test_l2_never_crosses_model_namespace(sessionmaker, redis):
    vectors = {"q": [1.0, 0.0, 0.0]}
    cache = _make_cache(sessionmaker, redis, vectors, threshold=0.9)
    await cache.put(
        model="model-a", prompt_hash="h1", prompt_text="q",
        content="answer-a", tokens_in=1, tokens_out=1,
    )
    hit = await cache.get(model="model-b", prompt_hash="h2", prompt_text="q")
    assert hit is None


async def test_get_on_empty_cache_returns_none(sessionmaker, redis):
    cache = _make_cache(sessionmaker, redis, vectors={"q": [1.0, 0.0, 0.0]}, threshold=0.9)
    assert await cache.get(model="m", prompt_hash="h", prompt_text="q") is None


async def test_missing_embeddings_disables_l2_without_erroring(sessionmaker, redis):
    l1 = ExactCache(redis, ttl_seconds=60)
    l2 = SemanticCache(sessionmaker, lambda: None, similarity_threshold=0.9)
    cache = TwoTierCache(l1, l2)
    # No L1 entry either, so this exercises the "L2 unavailable" path.
    assert await cache.get(model="m", prompt_hash="h", prompt_text="anything") is None
    await cache.put(
        model="m", prompt_hash="h", prompt_text="anything",
        content="answer", tokens_in=1, tokens_out=1,
    )  # must not raise even though L2 can't embed
    hit = await cache.get(model="m", prompt_hash="h", prompt_text="anything")
    assert hit is not None and hit.tier == CacheTier.L1  # L1 still works
