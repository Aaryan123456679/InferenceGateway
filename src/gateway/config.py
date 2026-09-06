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
