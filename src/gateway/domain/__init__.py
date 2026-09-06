"""Domain model — frozen dataclasses mirroring the DDL (persistence/models.py
is the SQLAlchemy mapping of these same shapes). Kept free of ORM/framework
imports so cache/routing/resilience can depend on plain data (DIP)."""
from __future__ import annotations

import enum
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID


class BackendType(enum.StrEnum):
    OLLAMA = "ollama"
    HOSTED = "hosted"


class CacheTier(enum.StrEnum):
    L1 = "l1"
    L2 = "l2"


@dataclass(frozen=True)
class ApiKey:
    id: UUID
    key_hash: str
    name: str
    token_budget: int
    window_seconds: int
    enabled: bool
    created_at: datetime


@dataclass(frozen=True)
class Backend:
    id: UUID
    name: str
    type: BackendType
    endpoint: str
    model: str
    priority: int
    enabled: bool
    cost_per_1k_in: float
    cost_per_1k_out: float
    created_at: datetime


@dataclass(frozen=True)
class CacheEntry:
    id: UUID
    model: str
    prompt_hash: str
    embedding: list[float]
    response: str
    tokens_in: int
    tokens_out: int
    created_at: datetime


@dataclass(frozen=True)
class RequestLogEntry:
    id: UUID
    api_key_id: UUID
    model: str
    tokens_in: int
    tokens_out: int
    cost: float
    latency_ms: int
    status: int
    created_at: datetime
    backend_id: UUID | None = None
    cache_tier: CacheTier | None = None
    finish_reason: str | None = None
