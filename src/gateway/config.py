"""Env-driven settings. All config flows through this one class (B.9)."""
from __future__ import annotations

from aikit.config import BaseServiceSettings


class GatewaySettings(BaseServiceSettings):
    admin_key: str
    ollama_host: str = "http://localhost:11434"
    hosted_api_base: str | None = None
    hosted_api_key: str | None = None
    cache_similarity_threshold: float = 0.97
    cache_ttl_seconds: int = 3600
    breaker_failure_threshold: int = 5
    breaker_cooldown_seconds: int = 30
    request_timeout_seconds: float = 30.0
    per_backend_concurrency: int = 16
    default_policy: str = "latency"
    # Sized above SQLAlchemy's own defaults (5/10): every request logs to
    # Postgres, so the DB pool - not just per_backend_concurrency - bounds
    # real concurrency. Found via load-testing at 50 concurrent users,
    # where the default 15-connection ceiling caused ~44% of requests to
    # fail on QueuePool timeout even against an instant backend.
    db_pool_size: int = 20
    db_max_overflow: int = 30
