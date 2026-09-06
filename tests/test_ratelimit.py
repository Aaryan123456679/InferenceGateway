from __future__ import annotations

from gateway.resilience.ratelimit import RateLimiter


async def test_reserve_allows_under_budget(redis):
    limiter = RateLimiter(redis)
    reservation = await limiter.reserve("key1", 50, budget=100, window_seconds=60)
    assert reservation is not None


async def test_reserve_denies_over_budget(redis):
    limiter = RateLimiter(redis)
    assert await limiter.reserve("key1", 80, budget=100, window_seconds=60) is not None
    assert await limiter.reserve("key1", 30, budget=100, window_seconds=60) is None


async def test_reconcile_corrects_reservation_cost(redis):
    limiter = RateLimiter(redis)
    reservation = await limiter.reserve("key1", 10, budget=100, window_seconds=60)
    assert reservation is not None
    await limiter.reconcile("key1", reservation, actual_tokens=90)

    # The corrected cost (90) should now count against the budget.
    assert await limiter.reserve("key1", 20, budget=100, window_seconds=60) is None


async def test_reconcile_can_free_up_budget_when_actual_is_lower(redis):
    limiter = RateLimiter(redis)
    reservation = await limiter.reserve("key1", 90, budget=100, window_seconds=60)
    assert reservation is not None
    await limiter.reconcile("key1", reservation, actual_tokens=10)

    assert await limiter.reserve("key1", 80, budget=100, window_seconds=60) is not None


async def test_independent_keys_have_independent_budgets(redis):
    limiter = RateLimiter(redis)
    assert await limiter.reserve("key1", 100, budget=100, window_seconds=60) is not None
    assert await limiter.reserve("key2", 100, budget=100, window_seconds=60) is not None


async def test_window_rollover_frees_budget(redis):
    limiter = RateLimiter(redis)
    # window_seconds=0 means every entry is immediately outside the window
    # on the next reserve() call's trim step.
    assert await limiter.reserve("key1", 100, budget=100, window_seconds=0) is not None
    assert await limiter.reserve("key1", 100, budget=100, window_seconds=0) is not None
