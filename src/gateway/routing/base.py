from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from gateway.domain import Backend


@dataclass(frozen=True)
class RequestFeatures:
    model: str
    est_tokens_in: int
    complexity: float  # cheap heuristic in v1 (length-based, see orchestrator)


@runtime_checkable
class RoutingPolicy(Protocol):
    """ISP: one method. `candidates` is already filtered to enabled backends
    whose circuit breaker currently allows traffic, in priority order."""

    name: str

    async def select(self, candidates: list[Backend], features: RequestFeatures) -> Backend:
        """Async because LatencyAwarePolicy reads rolling stats from Redis;
        CostTieredPolicy just never awaits anything internally."""
        ...
