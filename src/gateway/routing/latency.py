"""LatencyAwarePolicy — ranks candidates by rolling p95 latency, picks the
lowest. Called once per failover iteration on the shrinking candidate list
(see orchestrator), so repeated calls naturally walk best-to-worst."""
from __future__ import annotations

from gateway.domain import Backend
from gateway.resilience.latency_tracker import LatencyTracker
from gateway.routing.base import RequestFeatures


class LatencyAwarePolicy:
    name = "latency"

    def __init__(self, tracker: LatencyTracker) -> None:
        self._tracker = tracker

    async def select(self, candidates: list[Backend], features: RequestFeatures) -> Backend:
        if len(candidates) == 1:
            return candidates[0]
        scored = [(await self._tracker.p95(str(b.id)), b) for b in candidates]
        # A cold (no-data) backend gets p95=None, treated as best-case so new
        # or rarely-used backends get a chance rather than starving forever.
        scored.sort(key=lambda pair: (pair[0] is not None, pair[0] or 0.0))
        return scored[0][1]
