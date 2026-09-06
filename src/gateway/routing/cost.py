"""CostTieredPolicy — short/simple prompts route to the cheapest backend;
long/complex prompts route to the most capable (highest cost_per_1k, taken
as a capability proxy in v1). Complexity is a length heuristic computed by
the orchestrator; a learned router is future work (HLD A.2 non-goal)."""
from __future__ import annotations

from gateway.domain import Backend
from gateway.routing.base import RequestFeatures


class CostTieredPolicy:
    name = "cost"

    def __init__(self, complexity_threshold: float = 0.5) -> None:
        self._threshold = complexity_threshold

    async def select(self, candidates: list[Backend], features: RequestFeatures) -> Backend:
        by_cost = sorted(candidates, key=lambda b: b.cost_per_1k_in + b.cost_per_1k_out)
        return by_cost[0] if features.complexity < self._threshold else by_cost[-1]
