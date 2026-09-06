"""FastAPI app factory. Wires aikit clients + gateway services onto
`app.state` once at startup; routers/deps only ever read from there (B.1, B.8)."""
from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from aikit.db import make_engine, make_sessionmaker
from aikit.embeddings import EmbeddingService
from aikit.observability import PrometheusMetricsSink, configure_logging, get_logger
from fastapi import FastAPI
from redis.asyncio import Redis

from gateway.api.routers import admin_router, router, system_router
from gateway.backends.registry import BackendRegistry, default_client_factory
from gateway.cache.cache import TwoTierCache
from gateway.cache.exact import ExactCache
from gateway.cache.semantic import SemanticCache
from gateway.config import GatewaySettings
from gateway.orchestrator import RequestOrchestrator
from gateway.resilience.breaker import CircuitBreaker
from gateway.resilience.latency_tracker import LatencyTracker
from gateway.resilience.ratelimit import RateLimiter
from gateway.routing.base import RoutingPolicy
from gateway.routing.cost import CostTieredPolicy
from gateway.routing.latency import LatencyAwarePolicy

log = get_logger(__name__)


def _build_embeddings() -> EmbeddingService | None:
    try:
        from aikit.embeddings.sentence_transformer import SentenceTransformerEmbeddingService

        return SentenceTransformerEmbeddingService()
    except Exception as exc:  # noqa: BLE001 - L2 cache becomes unavailable, gateway still starts
        log.warning("embeddings_unavailable", error=str(exc))
        return None


def _build_policy(name: str, latency_tracker: LatencyTracker) -> RoutingPolicy:
    if name == "cost":
        return CostTieredPolicy()
    return LatencyAwarePolicy(latency_tracker)


def create_app(settings: GatewaySettings | None = None) -> FastAPI:
    settings = settings or GatewaySettings()  # type: ignore[call-arg]  # admin_key from env
    configure_logging(settings.log_level)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        engine = make_engine(settings.database_url)
        sessionmaker = make_sessionmaker(engine)
        redis = Redis.from_url(settings.redis_url)

        registry = BackendRegistry(
            sessionmaker,
            lambda backend: default_client_factory(
                backend, ollama_host=settings.ollama_host, hosted_api_key=settings.hosted_api_key
            ),
        )
        await registry.reload()

        l1 = ExactCache(redis, ttl_seconds=settings.cache_ttl_seconds)
        l2 = SemanticCache(
            sessionmaker,
            _build_embeddings,
            similarity_threshold=settings.cache_similarity_threshold,
        )
        cache = TwoTierCache(l1, l2)

        breaker = CircuitBreaker(
            redis,
            failure_threshold=settings.breaker_failure_threshold,
            cooldown_seconds=settings.breaker_cooldown_seconds,
        )
        rate_limiter = RateLimiter(redis)
        latency_tracker = LatencyTracker(redis)
        policy = _build_policy(settings.default_policy, latency_tracker)
        metrics = PrometheusMetricsSink()

        orchestrator = RequestOrchestrator(
            sessionmaker, registry, cache, breaker, rate_limiter, latency_tracker, policy,
            request_timeout_seconds=settings.request_timeout_seconds,
            per_backend_concurrency=settings.per_backend_concurrency,
            metrics=metrics,
        )

        app.state.settings = settings
        app.state.engine = engine
        app.state.sessionmaker = sessionmaker
        app.state.redis = redis
        app.state.registry = registry
        app.state.orchestrator = orchestrator
        app.state.metrics = metrics

        yield

        await registry.aclose()
        await redis.aclose()
        await engine.dispose()

    app = FastAPI(title="inference-gateway", lifespan=lifespan)
    app.include_router(router)
    app.include_router(admin_router)
    app.include_router(system_router)
    return app


app = create_app()
