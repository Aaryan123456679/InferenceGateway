"""Resilience — circuit breaker, token-budget rate limiter, latency
tracker. All Redis+Lua so state transitions are atomic across concurrent
gateway instances (B.5, B.6)."""
