from __future__ import annotations

from gateway.resilience.breaker import CircuitBreaker


async def test_closed_by_default(redis):
    breaker = CircuitBreaker(redis, failure_threshold=3, cooldown_seconds=30)
    assert await breaker.allow("b1") is True


async def test_opens_after_threshold_failures(redis):
    breaker = CircuitBreaker(redis, failure_threshold=3, cooldown_seconds=30)
    for _ in range(2):
        await breaker.record("b1", ok=False)
    assert await breaker.allow("b1") is True  # still under threshold

    await breaker.record("b1", ok=False)  # 3rd failure trips it
    assert await breaker.allow("b1") is False


async def test_success_resets_failure_count(redis):
    breaker = CircuitBreaker(redis, failure_threshold=3, cooldown_seconds=30)
    await breaker.record("b1", ok=False)
    await breaker.record("b1", ok=False)
    await breaker.record("b1", ok=True)  # resets failures to 0
    await breaker.record("b1", ok=False)
    await breaker.record("b1", ok=False)
    assert await breaker.allow("b1") is True  # only 2 consecutive failures since reset


async def test_recovers_through_half_open_to_closed(redis):
    breaker = CircuitBreaker(redis, failure_threshold=1, cooldown_seconds=0)
    await breaker.record("b1", ok=False)  # threshold=1 -> opens immediately

    # cooldown=0: the very next allow() call transitions open -> half_open
    # and lets the probe through.
    assert await breaker.allow("b1") is True
    state = await breaker.record("b1", ok=True)  # probe succeeds -> closed
    assert state == "closed"
    assert await breaker.allow("b1") is True


async def test_half_open_failure_reopens(redis):
    breaker = CircuitBreaker(redis, failure_threshold=1, cooldown_seconds=0)
    await breaker.record("b1", ok=False)  # open
    assert await breaker.allow("b1") is True  # transitions to half_open, probe allowed
    state = await breaker.record("b1", ok=False)  # probe fails -> back to open
    assert state == "open"


async def test_independent_backends_have_independent_state(redis):
    breaker = CircuitBreaker(redis, failure_threshold=1, cooldown_seconds=30)
    await breaker.record("b1", ok=False)
    assert await breaker.allow("b1") is False
    assert await breaker.allow("b2") is True
