"""Full-pipeline integration tests against real Postgres + Redis, with a
deterministic FakeModelClient standing in for Ollama/Hosted (B.13 #8: verify
real framework dispatch/registration end-to-end rather than trusting only
unit doubles — the live Docker smoke test is what does that for the actual
ModelClient wire calls; here we prove the orchestrator's own logic: cache,
rate limit, failover, logging)."""
from __future__ import annotations

import pytest
from aikit.db import session_scope
from aikit.model_client import ChatMessage

from gateway.backends.registry import BackendRegistry
from gateway.cache.cache import TwoTierCache
from gateway.cache.exact import ExactCache
from gateway.cache.semantic import SemanticCache
from gateway.domain import ApiKey, BackendType
from gateway.orchestrator import (
    AllBackendsUnavailable,
    NoBackendsAvailable,
    RateLimitExceeded,
    RequestOrchestrator,
)
from gateway.persistence.repositories import ApiKeyRepo, BackendRepo
from gateway.resilience.breaker import CircuitBreaker
from gateway.resilience.latency_tracker import LatencyTracker
from gateway.resilience.ratelimit import RateLimiter
from gateway.routing.latency import LatencyAwarePolicy
from tests.fakes import FakeModelClient


async def _make_api_key(sessionmaker, *, token_budget: int = 10_000) -> ApiKey:
    async with session_scope(sessionmaker) as session:
        return await ApiKeyRepo(session).create(
            name="test-key", key_hash="hash", token_budget=token_budget, window_seconds=60
        )


def _wire(sessionmaker, redis, client_by_name: dict[str, FakeModelClient]):
    registry = BackendRegistry(sessionmaker, lambda backend: client_by_name[backend.name])
    l1 = ExactCache(redis, ttl_seconds=60)
    l2 = SemanticCache(sessionmaker, lambda: None, similarity_threshold=0.97)  # L2 disabled
    cache = TwoTierCache(l1, l2)
    breaker = CircuitBreaker(redis, failure_threshold=1, cooldown_seconds=30)
    rate_limiter = RateLimiter(redis)
    latency_tracker = LatencyTracker(redis)
    policy = LatencyAwarePolicy(latency_tracker)
    orchestrator = RequestOrchestrator(
        sessionmaker, registry, cache, breaker, rate_limiter, latency_tracker, policy,
        request_timeout_seconds=5.0, per_backend_concurrency=8,
    )
    return orchestrator, registry


async def test_full_happy_path(sessionmaker, redis):
    api_key = await _make_api_key(sessionmaker)
    async with session_scope(sessionmaker) as session:
        await BackendRepo(session).upsert(
            name="primary", type=BackendType.OLLAMA, endpoint="e", model="m", priority=100
        )
    orchestrator, registry = _wire(
        sessionmaker, redis, {"primary": FakeModelClient(lambda p: f"echo:{p}")}
    )
    await registry.reload()

    result = await orchestrator.handle_complete(
        api_key=api_key, model="m", messages=[ChatMessage(role="user", content="hi")]
    )
    assert result.content == "echo:hi"
    assert result.backend_name == "primary"
    assert result.cache_tier is None


async def test_repeat_request_hits_l1_cache(sessionmaker, redis):
    api_key = await _make_api_key(sessionmaker)
    async with session_scope(sessionmaker) as session:
        await BackendRepo(session).upsert(
            name="primary", type=BackendType.OLLAMA, endpoint="e", model="m"
        )
    client = FakeModelClient(lambda p: f"echo:{p}")
    orchestrator, registry = _wire(sessionmaker, redis, {"primary": client})
    await registry.reload()

    messages = [ChatMessage(role="user", content="hi")]
    first = await orchestrator.handle_complete(api_key=api_key, model="m", messages=messages)
    second = await orchestrator.handle_complete(api_key=api_key, model="m", messages=messages)

    assert first.content == second.content
    assert second.cache_tier is not None and second.cache_tier.value == "l1"
    assert len(client.calls) == 1, "second request must be served from cache, not the backend"


async def test_rate_limit_exceeded_raises(sessionmaker, redis):
    api_key = await _make_api_key(sessionmaker, token_budget=1)
    async with session_scope(sessionmaker) as session:
        await BackendRepo(session).upsert(
            name="primary", type=BackendType.OLLAMA, endpoint="e", model="m"
        )
    orchestrator, registry = _wire(sessionmaker, redis, {"primary": FakeModelClient()})
    await registry.reload()

    with pytest.raises(RateLimitExceeded):
        await orchestrator.handle_complete(
            api_key=api_key, model="m", messages=[ChatMessage(role="user", content="x" * 100)]
        )


async def test_no_backends_for_model_raises(sessionmaker, redis):
    api_key = await _make_api_key(sessionmaker)
    orchestrator, registry = _wire(sessionmaker, redis, {})
    await registry.reload()

    with pytest.raises(NoBackendsAvailable):
        await orchestrator.handle_complete(
            api_key=api_key, model="nonexistent", messages=[ChatMessage(role="user", content="hi")]
        )


async def test_failover_to_second_backend_on_first_failure(sessionmaker, redis):
    api_key = await _make_api_key(sessionmaker)
    async with session_scope(sessionmaker) as session:
        repo = BackendRepo(session)
        await repo.upsert(
            name="broken", type=BackendType.OLLAMA, endpoint="e", model="m", priority=100
        )
        await repo.upsert(
            name="healthy", type=BackendType.OLLAMA, endpoint="e", model="m", priority=200
        )

    broken = FakeModelClient(fail=True, name="broken")
    healthy = FakeModelClient(lambda p: "ok", name="healthy")
    orchestrator, registry = _wire(sessionmaker, redis, {"broken": broken, "healthy": healthy})
    await registry.reload()

    result = await orchestrator.handle_complete(
        api_key=api_key, model="m", messages=[ChatMessage(role="user", content="hi")]
    )
    assert result.backend_name == "healthy"
    assert result.content == "ok"


async def test_all_backends_failing_raises(sessionmaker, redis):
    api_key = await _make_api_key(sessionmaker)
    async with session_scope(sessionmaker) as session:
        await BackendRepo(session).upsert(
            name="broken", type=BackendType.OLLAMA, endpoint="e", model="m"
        )
    orchestrator, registry = _wire(sessionmaker, redis, {"broken": FakeModelClient(fail=True)})
    await registry.reload()

    with pytest.raises(AllBackendsUnavailable):
        await orchestrator.handle_complete(
            api_key=api_key, model="m", messages=[ChatMessage(role="user", content="hi")]
        )


async def test_repeated_failures_trip_the_breaker(sessionmaker, redis):
    """After enough failures the breaker opens, so a subsequent request never
    even attempts the broken backend (verified via call count)."""
    api_key = await _make_api_key(sessionmaker)
    async with session_scope(sessionmaker) as session:
        await BackendRepo(session).upsert(
            name="broken", type=BackendType.OLLAMA, endpoint="e", model="m"
        )
    broken = FakeModelClient(fail=True, name="broken")
    registry = BackendRegistry(sessionmaker, lambda backend: broken)
    l1 = ExactCache(redis, ttl_seconds=60)
    l2 = SemanticCache(sessionmaker, lambda: None, similarity_threshold=0.97)
    cache = TwoTierCache(l1, l2)
    breaker = CircuitBreaker(redis, failure_threshold=1, cooldown_seconds=9999)
    rate_limiter = RateLimiter(redis)
    latency_tracker = LatencyTracker(redis)
    policy = LatencyAwarePolicy(latency_tracker)
    orchestrator = RequestOrchestrator(
        sessionmaker, registry, cache, breaker, rate_limiter, latency_tracker, policy,
        request_timeout_seconds=5.0, per_backend_concurrency=8,
    )
    await registry.reload()

    with pytest.raises(AllBackendsUnavailable):
        await orchestrator.handle_complete(
            api_key=api_key, model="m", messages=[ChatMessage(role="user", content="hi")]
        )
    calls_after_first = len(broken.calls)

    with pytest.raises(AllBackendsUnavailable):
        await orchestrator.handle_complete(
            api_key=api_key, model="m", messages=[ChatMessage(role="user", content="hi2")]
        )
    assert len(broken.calls) == calls_after_first, "breaker should skip the call entirely once open"


async def test_stream_happy_path_yields_text_then_result(sessionmaker, redis):
    api_key = await _make_api_key(sessionmaker)
    async with session_scope(sessionmaker) as session:
        await BackendRepo(session).upsert(
            name="primary", type=BackendType.OLLAMA, endpoint="e", model="m"
        )
    orchestrator, registry = _wire(
        sessionmaker, redis, {"primary": FakeModelClient(lambda p: f"echo:{p}")}
    )
    await registry.reload()

    events = [
        event
        async for event in orchestrator.handle_stream(
            api_key=api_key, model="m", messages=[ChatMessage(role="user", content="hi")]
        )
    ]
    *chunks, final = events
    assert "".join(chunks) == "echo:hi"
    assert final.backend_name == "primary"
    assert final.finish_reason == "stop"


async def test_stream_failure_after_yield_does_not_silently_failover(sessionmaker, redis):
    """A backend that fails mid-stream must not have its partial output
    silently followed by a second backend's output - that would corrupt
    what the client already received."""

    class FlakyStreamClient(FakeModelClient):
        async def stream(self, messages, *, model=None):
            yield "partial "
            raise ConnectionError("dropped mid-stream")

    api_key = await _make_api_key(sessionmaker)
    async with session_scope(sessionmaker) as session:
        repo = BackendRepo(session)
        await repo.upsert(
            name="flaky", type=BackendType.OLLAMA, endpoint="e", model="m", priority=100
        )
        await repo.upsert(
            name="backup", type=BackendType.OLLAMA, endpoint="e", model="m", priority=200
        )

    flaky = FlakyStreamClient(name="flaky")
    backup = FakeModelClient(lambda p: "should not be reached", name="backup")
    orchestrator, registry = _wire(sessionmaker, redis, {"flaky": flaky, "backup": backup})
    await registry.reload()

    received: list[str] = []
    with pytest.raises(AllBackendsUnavailable):
        async for event in orchestrator.handle_stream(
            api_key=api_key, model="m", messages=[ChatMessage(role="user", content="hi")]
        ):
            received.append(event)  # type: ignore[arg-type]

    assert received == ["partial "]
    assert backup.calls == [], "must not have tried the backup backend after bytes were sent"
