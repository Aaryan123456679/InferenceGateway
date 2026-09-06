from __future__ import annotations

from gateway.resilience.latency_tracker import LatencyTracker


async def test_cold_backend_reports_none(redis):
    tracker = LatencyTracker(redis)
    assert await tracker.p95("never-seen") is None


async def test_p95_over_known_distribution(redis):
    tracker = LatencyTracker(redis, window_size=100)
    for ms in range(1, 101):  # 1..100
        await tracker.record("backend-a", float(ms))
    p95 = await tracker.p95("backend-a")
    assert p95 == 96.0  # index int(100*0.95)=95 -> sorted[95] == 96


async def test_window_caps_at_window_size(redis):
    tracker = LatencyTracker(redis, window_size=5)
    for ms in range(1, 11):  # 1..10, only last 5 pushed values are kept
        await tracker.record("backend-b", float(ms))
    p95 = await tracker.p95("backend-b")
    # LPUSH keeps most-recent-first; LTRIM 0..4 keeps the 5 most recent
    assert p95 == 10.0
