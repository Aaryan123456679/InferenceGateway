from __future__ import annotations

import uuid
from datetime import UTC, datetime

from gateway.domain import Backend, BackendType
from gateway.resilience.latency_tracker import LatencyTracker
from gateway.routing.base import RequestFeatures
from gateway.routing.cost import CostTieredPolicy
from gateway.routing.latency import LatencyAwarePolicy


def _backend(name: str, *, cost_in: float, cost_out: float = 0.0, priority: int = 100) -> Backend:
    return Backend(
        id=uuid.uuid4(), name=name, type=BackendType.OLLAMA, endpoint="http://x", model="m",
        priority=priority, enabled=True, cost_per_1k_in=cost_in, cost_per_1k_out=cost_out,
        created_at=datetime.now(UTC),
    )


async def test_cost_policy_picks_cheapest_for_simple_prompts():
    cheap = _backend("cheap", cost_in=0.1)
    expensive = _backend("expensive", cost_in=10.0)
    policy = CostTieredPolicy(complexity_threshold=0.5)
    features = RequestFeatures(model="m", est_tokens_in=10, complexity=0.1)
    chosen = await policy.select([cheap, expensive], features)
    assert chosen.name == "cheap"


async def test_cost_policy_picks_priciest_for_complex_prompts():
    cheap = _backend("cheap", cost_in=0.1)
    expensive = _backend("expensive", cost_in=10.0)
    policy = CostTieredPolicy(complexity_threshold=0.5)
    features = RequestFeatures(model="m", est_tokens_in=1000, complexity=0.9)
    chosen = await policy.select([cheap, expensive], features)
    assert chosen.name == "expensive"


async def test_cost_policy_single_candidate_short_circuits():
    only = _backend("only", cost_in=1.0)
    policy = CostTieredPolicy()
    features = RequestFeatures(model="m", est_tokens_in=1, complexity=0.0)
    assert (await policy.select([only], features)).name == "only"


async def test_latency_policy_picks_lowest_p95(redis):
    fast = _backend("fast", cost_in=0.0)
    slow = _backend("slow", cost_in=0.0)
    tracker = LatencyTracker(redis)
    for _ in range(5):
        await tracker.record(str(fast.id), 10.0)
        await tracker.record(str(slow.id), 500.0)

    policy = LatencyAwarePolicy(tracker)
    features = RequestFeatures(model="m", est_tokens_in=1, complexity=0.0)
    chosen = await policy.select([slow, fast], features)
    assert chosen.name == "fast"


async def test_latency_policy_gives_cold_backend_a_chance(redis):
    """A backend with no recorded latency (p95=None) should be preferred
    over one with a bad recorded p95, so new backends aren't starved."""
    warmed_slow = _backend("warmed_slow", cost_in=0.0)
    cold = _backend("cold", cost_in=0.0)
    tracker = LatencyTracker(redis)
    await tracker.record(str(warmed_slow.id), 5000.0)

    policy = LatencyAwarePolicy(tracker)
    features = RequestFeatures(model="m", est_tokens_in=1, complexity=0.0)
    chosen = await policy.select([warmed_slow, cold], features)
    assert chosen.name == "cold"
