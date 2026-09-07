"""RequestOrchestrator — the request pipeline (B.4): auth is done by the
caller (api/deps.py); this takes it from rate-limit reserve through cache,
routing, failover, and logging. DIP: depends only on the cache/breaker/
limiter/tracker/policy abstractions and aikit's ModelClient, never on Redis
or a specific backend concretely."""
from __future__ import annotations

import asyncio
import hashlib
import time
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

from aikit.db import session_scope
from aikit.model_client import ChatMessage, Usage
from aikit.observability import MetricsSink
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from gateway.backends.registry import BackendRegistry
from gateway.cache.cache import TwoTierCache
from gateway.domain import ApiKey, CacheTier
from gateway.persistence.repositories import RequestLogRepo
from gateway.resilience.breaker import CircuitBreaker
from gateway.resilience.latency_tracker import LatencyTracker
from gateway.resilience.ratelimit import RateLimiter
from gateway.routing.base import RequestFeatures, RoutingPolicy


class RateLimitExceeded(Exception):
    pass


class NoBackendsAvailable(Exception):
    pass


class AllBackendsUnavailable(Exception):
    def __init__(self, last_error: Exception | None) -> None:
        super().__init__("all candidate backends failed or are circuit-open")
        self.last_error = last_error


@dataclass(frozen=True)
class CompletionResult:
    content: str
    model: str
    tokens_in: int
    tokens_out: int
    finish_reason: str | None
    cache_tier: CacheTier | None
    backend_name: str | None


def estimate_tokens(messages: list[ChatMessage]) -> int:
    """Cheap, provider-agnostic heuristic: ~4 chars/token (roughly true for
    English text across most tokenizers). Corrected by reconcile() once the
    real usage is known."""
    total_chars = sum(len(m.content) for m in messages)
    return max(1, total_chars // 4)


def estimate_complexity(messages: list[ChatMessage]) -> float:
    """Normalized [0,1] length heuristic driving CostTieredPolicy. A
    learned router is out of scope for v1 (HLD A.2)."""
    total_chars = sum(len(m.content) for m in messages)
    return min(1.0, total_chars / 2000)


def prompt_hash(model: str, messages: list[ChatMessage]) -> str:
    raw = model + "|" + "|".join(f"{m.role}:{m.content}" for m in messages)
    return hashlib.sha256(raw.encode()).hexdigest()


def prompt_text(messages: list[ChatMessage]) -> str:
    return "\n".join(m.content for m in messages)


class RequestOrchestrator:
    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        registry: BackendRegistry,
        cache: TwoTierCache,
        breaker: CircuitBreaker,
        rate_limiter: RateLimiter,
        latency_tracker: LatencyTracker,
        policy: RoutingPolicy,
        *,
        request_timeout_seconds: float,
        per_backend_concurrency: int,
        metrics: MetricsSink | None = None,
    ) -> None:
        self._sessionmaker = sessionmaker
        self._registry = registry
        self._cache = cache
        self._breaker = breaker
        self._rate_limiter = rate_limiter
        self._latency_tracker = latency_tracker
        self._policy = policy
        self._timeout = request_timeout_seconds
        self._per_backend_concurrency = per_backend_concurrency
        self._metrics = metrics
        self._semaphores: dict[uuid.UUID, asyncio.Semaphore] = {}

    def _semaphore_for(self, backend_id: uuid.UUID) -> asyncio.Semaphore:
        sem = self._semaphores.get(backend_id)
        if sem is None:
            sem = asyncio.Semaphore(self._per_backend_concurrency)
            self._semaphores[backend_id] = sem
        return sem

    async def _log(
        self, *, api_key: ApiKey, model: str, latency_ms: int, status: int, **kw: Any
    ) -> None:
        async with session_scope(self._sessionmaker) as session:
            await RequestLogRepo(session).insert(
                api_key_id=api_key.id, model=model, latency_ms=latency_ms, status=status, **kw
            )

    async def handle_complete(
        self,
        *,
        api_key: ApiKey,
        model: str,
        messages: list[ChatMessage],
        bypass_cache: bool = False,
    ) -> CompletionResult:
        start = time.perf_counter()
        est_tokens = estimate_tokens(messages)
        reservation = await self._rate_limiter.reserve(
            str(api_key.id), est_tokens,
            budget=api_key.token_budget, window_seconds=api_key.window_seconds,
        )
        if reservation is None:
            raise RateLimitExceeded()

        p_hash = prompt_hash(model, messages)
        p_text = prompt_text(messages)

        # bypass_cache (routers.py: `x-cache: no-store` request header) is
        # a control for A/B load testing - it skips both the read and the
        # write-back below, so a baseline run neither serves from nor
        # pollutes the cache with synthetic traffic.
        hit = (
            None
            if bypass_cache
            else await self._cache.get(model=model, prompt_hash=p_hash, prompt_text=p_text)
        )
        if hit is not None:
            await self._rate_limiter.reconcile(
                str(api_key.id), reservation, hit.tokens_in + hit.tokens_out
            )
            latency_ms = int((time.perf_counter() - start) * 1000)
            await self._log(
                api_key=api_key, model=model, latency_ms=latency_ms, status=200,
                cache_tier=hit.tier.value, tokens_in=hit.tokens_in, tokens_out=hit.tokens_out,
            )
            if self._metrics:
                self._metrics.incr("gateway_cache_hits_total", tier=hit.tier.value)
            return CompletionResult(
                content=hit.content, model=model,
                tokens_in=hit.tokens_in, tokens_out=hit.tokens_out,
                finish_reason=None, cache_tier=hit.tier, backend_name=None,
            )

        remaining = self._registry.candidates(model)
        if not remaining:
            raise NoBackendsAvailable()

        features = RequestFeatures(
            model=model, est_tokens_in=est_tokens, complexity=estimate_complexity(messages)
        )
        last_error: Exception | None = None

        while remaining:
            chosen = await self._policy.select([b for b, _ in remaining], features)
            backend, client = next(pair for pair in remaining if pair[0].id == chosen.id)
            remaining = [pair for pair in remaining if pair[0].id != chosen.id]

            if not await self._breaker.allow(str(backend.id)):
                continue

            try:
                async with self._semaphore_for(backend.id):
                    async with asyncio.timeout(self._timeout):
                        completion = await client.complete(messages, model=backend.model)
            except Exception as exc:  # noqa: BLE001 - failover walk tries the next candidate
                last_error = exc
                await self._breaker.record(str(backend.id), ok=False)
                if self._metrics:
                    self._metrics.incr(
                        "gateway_backend_calls_total", backend=backend.name, outcome="error"
                    )
                continue

            latency_ms = int((time.perf_counter() - start) * 1000)
            await self._breaker.record(str(backend.id), ok=True)
            await self._latency_tracker.record(str(backend.id), latency_ms)
            if not bypass_cache:
                await self._cache.put(
                    model=model, prompt_hash=p_hash, prompt_text=p_text,
                    content=completion.content,
                    tokens_in=completion.tokens_in, tokens_out=completion.tokens_out,
                )
            await self._rate_limiter.reconcile(
                str(api_key.id), reservation, completion.tokens_in + completion.tokens_out
            )
            cost = (
                backend.cost_per_1k_in * completion.tokens_in / 1000
                + backend.cost_per_1k_out * completion.tokens_out / 1000
            )
            await self._log(
                api_key=api_key, model=model, latency_ms=latency_ms, status=200,
                backend_id=backend.id,
                tokens_in=completion.tokens_in, tokens_out=completion.tokens_out,
                cost=cost, finish_reason="stop",
            )
            if self._metrics:
                self._metrics.incr(
                    "gateway_backend_calls_total", backend=backend.name, outcome="ok"
                )
                self._metrics.observe(
                    "gateway_request_latency_seconds", latency_ms / 1000, cache="miss"
                )
            return CompletionResult(
                content=completion.content, model=backend.model, tokens_in=completion.tokens_in,
                tokens_out=completion.tokens_out, finish_reason="stop", cache_tier=None,
                backend_name=backend.name,
            )

        latency_ms = int((time.perf_counter() - start) * 1000)
        await self._log(api_key=api_key, model=model, latency_ms=latency_ms, status=503)
        if self._metrics:
            self._metrics.incr("gateway_failover_total")
        raise AllBackendsUnavailable(last_error)

    async def handle_stream(
        self,
        *,
        api_key: ApiKey,
        model: str,
        messages: list[ChatMessage],
        bypass_cache: bool = False,
    ) -> AsyncIterator[str | CompletionResult]:
        """Yields text chunks, then exactly one terminal `CompletionResult`
        (mirroring aikit's own str|Usage stream contract) carrying the
        accumulated content and usage for the caller to build the closing
        SSE frame.

        Failover is only safe *before* the first chunk reaches the caller —
        once a chunk has been yielded, a mid-stream backend failure ends the
        response rather than silently splicing in a second backend's output,
        which would corrupt what the client has already received.
        """
        start = time.perf_counter()
        est_tokens = estimate_tokens(messages)
        reservation = await self._rate_limiter.reserve(
            str(api_key.id), est_tokens,
            budget=api_key.token_budget, window_seconds=api_key.window_seconds,
        )
        if reservation is None:
            raise RateLimitExceeded()

        p_hash = prompt_hash(model, messages)
        p_text = prompt_text(messages)

        hit = (
            None
            if bypass_cache
            else await self._cache.get(model=model, prompt_hash=p_hash, prompt_text=p_text)
        )
        if hit is not None:
            await self._rate_limiter.reconcile(
                str(api_key.id), reservation, hit.tokens_in + hit.tokens_out
            )
            latency_ms = int((time.perf_counter() - start) * 1000)
            await self._log(
                api_key=api_key, model=model, latency_ms=latency_ms, status=200,
                cache_tier=hit.tier.value, tokens_in=hit.tokens_in, tokens_out=hit.tokens_out,
            )
            if self._metrics:
                self._metrics.incr("gateway_cache_hits_total", tier=hit.tier.value)
            yield hit.content
            yield CompletionResult(
                content=hit.content, model=model,
                tokens_in=hit.tokens_in, tokens_out=hit.tokens_out,
                finish_reason=None, cache_tier=hit.tier, backend_name=None,
            )
            return

        remaining = self._registry.candidates(model)
        if not remaining:
            raise NoBackendsAvailable()

        features = RequestFeatures(
            model=model, est_tokens_in=est_tokens, complexity=estimate_complexity(messages)
        )
        last_error: Exception | None = None

        while remaining:
            chosen = await self._policy.select([b for b, _ in remaining], features)
            backend, client = next(pair for pair in remaining if pair[0].id == chosen.id)
            remaining = [pair for pair in remaining if pair[0].id != chosen.id]

            if not await self._breaker.allow(str(backend.id)):
                continue

            accumulated: list[str] = []
            usage: Usage | None = None
            any_yielded = False
            try:
                async with self._semaphore_for(backend.id):
                    async with asyncio.timeout(self._timeout):
                        async for event in client.stream(messages, model=backend.model):
                            if isinstance(event, Usage):
                                usage = event
                            else:
                                accumulated.append(event)
                                any_yielded = True
                                yield event
            except Exception as exc:  # noqa: BLE001
                last_error = exc
                await self._breaker.record(str(backend.id), ok=False)
                if any_yielded:
                    raise AllBackendsUnavailable(exc) from exc
                continue

            content = "".join(accumulated)
            tokens_in = usage.tokens_in if usage else 0
            tokens_out = usage.tokens_out if usage else 0
            finish_reason = usage.finish_reason if usage else "stop"

            latency_ms = int((time.perf_counter() - start) * 1000)
            await self._breaker.record(str(backend.id), ok=True)
            await self._latency_tracker.record(str(backend.id), latency_ms)
            if not bypass_cache:
                await self._cache.put(
                    model=model, prompt_hash=p_hash, prompt_text=p_text, content=content,
                    tokens_in=tokens_in, tokens_out=tokens_out,
                )
            await self._rate_limiter.reconcile(
                str(api_key.id), reservation, tokens_in + tokens_out
            )
            cost = (
                backend.cost_per_1k_in * tokens_in / 1000
                + backend.cost_per_1k_out * tokens_out / 1000
            )
            await self._log(
                api_key=api_key, model=model, latency_ms=latency_ms, status=200,
                backend_id=backend.id, tokens_in=tokens_in, tokens_out=tokens_out, cost=cost,
                finish_reason=finish_reason,
            )
            if self._metrics:
                self._metrics.incr(
                    "gateway_backend_calls_total", backend=backend.name, outcome="ok"
                )
            yield CompletionResult(
                content=content, model=backend.model,
                tokens_in=tokens_in, tokens_out=tokens_out,
                finish_reason=finish_reason, cache_tier=None, backend_name=backend.name,
            )
            return

        latency_ms = int((time.perf_counter() - start) * 1000)
        await self._log(api_key=api_key, model=model, latency_ms=latency_ms, status=503)
        raise AllBackendsUnavailable(last_error)
