"""SQLAlchemy ORM mapping of the DDL in HLD+LLD.md B.2, built on aikit's
shared `Base`. Referential integrity and constraints live at the DB layer
(CHECK, UNIQUE, FK) rather than in application code."""
from __future__ import annotations

import enum
import uuid
from datetime import datetime

from aikit.db import Base
from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

EMBEDDING_DIM = 384  # all-MiniLM-L6-v2


class BackendType(enum.StrEnum):
    OLLAMA = "ollama"
    HOSTED = "hosted"


class CacheTier(enum.StrEnum):
    L1 = "l1"
    L2 = "l2"


def _str_enum_column(enum_cls: type[enum.StrEnum], name: str) -> Enum:
    return Enum(
        enum_cls, name=name, native_enum=False,
        values_callable=lambda cls: [e.value for e in cls],
    )


class ApiKeyModel(Base):
    __tablename__ = "api_key"
    __table_args__ = (
        CheckConstraint("token_budget > 0", name="ck_api_key_budget_positive"),
        CheckConstraint("window_seconds > 0", name="ck_api_key_window_positive"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    key_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    token_budget: Mapped[int] = mapped_column(Integer, nullable=False)
    window_seconds: Mapped[int] = mapped_column(Integer, nullable=False, default=60)
    enabled: Mapped[bool] = mapped_column(default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class BackendModel(Base):
    __tablename__ = "backend"

    id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    name: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    type: Mapped[BackendType] = mapped_column(
        _str_enum_column(BackendType, "backend_type"), nullable=False
    )
    endpoint: Mapped[str] = mapped_column(Text, nullable=False)
    model: Mapped[str] = mapped_column(Text, nullable=False)
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=100)
    enabled: Mapped[bool] = mapped_column(default=True)
    cost_per_1k_in: Mapped[float] = mapped_column(Numeric(10, 6), nullable=False, default=0)
    cost_per_1k_out: Mapped[float] = mapped_column(Numeric(10, 6), nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class CacheEntryModel(Base):
    __tablename__ = "cache_entry"

    id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    model: Mapped[str] = mapped_column(Text, nullable=False)
    prompt_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    embedding: Mapped[list[float]] = mapped_column(Vector(EMBEDDING_DIM), nullable=False)
    response: Mapped[str] = mapped_column(Text, nullable=False)
    tokens_in: Mapped[int] = mapped_column(Integer, nullable=False)
    tokens_out: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class RequestLogModel(Base):
    __tablename__ = "request_log"
    __table_args__ = (
        CheckConstraint("cache_tier IN ('l1','l2')", name="ck_log_cache_tier"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    api_key_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("api_key.id"), nullable=False
    )
    backend_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("backend.id"), nullable=True
    )
    cache_tier: Mapped[str | None] = mapped_column(Text, nullable=True)
    model: Mapped[str] = mapped_column(Text, nullable=False)
    tokens_in: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    tokens_out: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    cost: Mapped[float] = mapped_column(Numeric(12, 6), nullable=False, default=0)
    latency_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[int] = mapped_column(Integer, nullable=False)
    finish_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
