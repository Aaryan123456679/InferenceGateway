"""Repositories — the only layer that knows SQL/ORM. cache/backends/api
layers depend on these classes (not on raw sessions), so tests can swap in
fakes without touching Postgres (DIP)."""
from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from gateway.domain import (
    ApiKey,
    Backend,
    BackendType,
    CacheEntry,
    RequestLogEntry,
)
from gateway.persistence.models import (
    ApiKeyModel,
    BackendModel,
    CacheEntryModel,
    RequestLogModel,
)
from gateway.persistence.models import BackendType as ORMBackendType


def _api_key_to_domain(row: ApiKeyModel) -> ApiKey:
    return ApiKey(
        id=row.id,
        key_hash=row.key_hash,
        name=row.name,
        token_budget=row.token_budget,
        window_seconds=row.window_seconds,
        enabled=row.enabled,
        created_at=row.created_at,
    )


def _backend_to_domain(row: BackendModel) -> Backend:
    return Backend(
        id=row.id,
        name=row.name,
        type=BackendType(row.type),
        endpoint=row.endpoint,
        model=row.model,
        priority=row.priority,
        enabled=row.enabled,
        cost_per_1k_in=float(row.cost_per_1k_in),
        cost_per_1k_out=float(row.cost_per_1k_out),
        created_at=row.created_at,
    )


class ApiKeyRepo:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_by_hash(self, key_hash: str) -> ApiKey | None:
        row = await self._session.scalar(
            select(ApiKeyModel).where(ApiKeyModel.key_hash == key_hash, ApiKeyModel.enabled)
        )
        return _api_key_to_domain(row) if row else None

    async def create(
        self, *, name: str, key_hash: str, token_budget: int, window_seconds: int = 60
    ) -> ApiKey:
        row = ApiKeyModel(
            id=uuid.uuid4(),
            name=name,
            key_hash=key_hash,
            token_budget=token_budget,
            window_seconds=window_seconds,
        )
        self._session.add(row)
        await self._session.flush()
        return _api_key_to_domain(row)


class BackendRepo:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def list_enabled_for_model(self, model: str) -> list[Backend]:
        rows = (
            await self._session.execute(
                select(BackendModel)
                .where(BackendModel.model == model, BackendModel.enabled)
                .order_by(BackendModel.priority.asc())
            )
        ).scalars().all()
        return [_backend_to_domain(row) for row in rows]

    async def list_all(self) -> list[Backend]:
        rows = (await self._session.execute(select(BackendModel))).scalars().all()
        return [_backend_to_domain(row) for row in rows]

    async def upsert(
        self,
        *,
        name: str,
        type: BackendType,
        endpoint: str,
        model: str,
        priority: int = 100,
        cost_per_1k_in: float = 0.0,
        cost_per_1k_out: float = 0.0,
    ) -> Backend:
        existing = await self._session.scalar(
            select(BackendModel).where(BackendModel.name == name)
        )
        if existing is not None:
            existing.type = ORMBackendType(type.value)
            existing.endpoint = endpoint
            existing.model = model
            existing.priority = priority
            existing.cost_per_1k_in = cost_per_1k_in
            existing.cost_per_1k_out = cost_per_1k_out
            await self._session.flush()
            return _backend_to_domain(existing)

        row = BackendModel(
            id=uuid.uuid4(),
            name=name,
            type=ORMBackendType(type.value),
            endpoint=endpoint,
            model=model,
            priority=priority,
            cost_per_1k_in=cost_per_1k_in,
            cost_per_1k_out=cost_per_1k_out,
        )
        self._session.add(row)
        await self._session.flush()
        return _backend_to_domain(row)


class SemanticCacheRepo:
    """L2 cache persistence: pgvector cosine-similarity lookup namespaced by
    model (B.2, B.7 correctness note — never cross-serve models)."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def find_similar(
        self, *, model: str, embedding: list[float], threshold: float
    ) -> CacheEntry | None:
        # pgvector's `<=>` is cosine *distance*; similarity = 1 - distance.
        stmt = (
            select(
                CacheEntryModel,
                (1 - CacheEntryModel.embedding.cosine_distance(embedding)).label("similarity"),
            )
            .where(CacheEntryModel.model == model)
            .order_by(CacheEntryModel.embedding.cosine_distance(embedding))
            .limit(1)
        )
        row = (await self._session.execute(stmt)).first()
        if row is None:
            return None
        entry, similarity = row
        if similarity < threshold:
            return None
        return CacheEntry(
            id=entry.id,
            model=entry.model,
            prompt_hash=entry.prompt_hash,
            embedding=list(entry.embedding),
            response=entry.response,
            tokens_in=entry.tokens_in,
            tokens_out=entry.tokens_out,
            created_at=entry.created_at,
        )

    async def insert(
        self,
        *,
        model: str,
        prompt_hash: str,
        embedding: list[float],
        response: str,
        tokens_in: int,
        tokens_out: int,
    ) -> None:
        self._session.add(
            CacheEntryModel(
                id=uuid.uuid4(),
                model=model,
                prompt_hash=prompt_hash,
                embedding=embedding,
                response=response,
                tokens_in=tokens_in,
                tokens_out=tokens_out,
            )
        )
        await self._session.flush()


class RequestLogRepo:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def insert(
        self,
        *,
        api_key_id: uuid.UUID,
        model: str,
        latency_ms: int,
        status: int,
        backend_id: uuid.UUID | None = None,
        cache_tier: str | None = None,
        tokens_in: int = 0,
        tokens_out: int = 0,
        cost: float = 0.0,
        finish_reason: str | None = None,
    ) -> RequestLogEntry:
        row = RequestLogModel(
            id=uuid.uuid4(),
            api_key_id=api_key_id,
            backend_id=backend_id,
            cache_tier=cache_tier,
            model=model,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            cost=cost,
            latency_ms=latency_ms,
            status=status,
            finish_reason=finish_reason,
        )
        self._session.add(row)
        await self._session.flush()
        return RequestLogEntry(
            id=row.id,
            api_key_id=row.api_key_id,
            model=row.model,
            tokens_in=row.tokens_in,
            tokens_out=row.tokens_out,
            cost=float(row.cost),
            latency_ms=row.latency_ms,
            status=row.status,
            created_at=row.ts,
            backend_id=row.backend_id,
            cache_tier=row.cache_tier,  # type: ignore[arg-type]
            finish_reason=row.finish_reason,
        )
