# Gateway load test

Two separate questions, two separate tests. Raw results and a script that
derives every number from them (not hand-typed) live in
[`results/`](results/) - run `python3 summarize.py` to regenerate.

## 1. Does the cache reduce latency?

```bash
make load-baseline   # cache OFF (x-cache: no-store) - the control
make load             # cache ON
```

Same traffic, run twice. Each prints an aggregated p95 (ms) and a cache
hit-rate over its own window; the reduction is `(p95_baseline - p95_cache)
/ p95_baseline`.

## 2. Was the gateway itself tested under real concurrency?

The answer to #1 is only as strong as its concurrency is real - and a
single small model on one CPU-only Ollama instance can't sustain many
concurrent users, which caps how many you can honestly run test #1 at.
That's a real limit on *that* test, but it says nothing about the
gateway's own pipeline (auth, rate limiting, cache I/O, routing, breaker
checks, request logging) independent of model latency - so test that
separately, against something that isn't slow:

```bash
uvicorn loadtest.stub_backend:app --port 11500 &   # instant Ollama-contract stub
# register it as a backend (model: "stub"), then:
USERS=50 SPAWN=10 DURATION=2m LABEL=gateway_concurrency NO_CACHE=1 \
  bash loadtest/run.sh
```
`NO_CACHE=1` here isn't about the cache - it forces every request through
the *full* uncached pipeline (rate-limit reserve/reconcile, routing,
breaker, request-log write), not a cheap L1 short-circuit, so this
measures the gateway's actual per-request overhead at real concurrency.

## Setup

```bash
docker compose -f docker/docker-compose.yml up -d postgres redis ollama
alembic upgrade head
docker compose -f docker/docker-compose.yml exec ollama ollama pull qwen2.5:0.5b
uvicorn gateway.main:app &   # or run the api container instead

curl -s -X POST http://localhost:8000/admin/backends \
  -H "X-Admin-Key: $ADMIN_KEY" -H "Content-Type: application/json" \
  -d '{"name":"primary","type":"ollama","endpoint":"http://localhost:11435","model":"qwen2.5:0.5b"}'

curl -s -X POST http://localhost:8000/admin/keys \
  -H "X-Admin-Key: $ADMIN_KEY" -H "Content-Type: application/json" \
  -d '{"name":"loadtest","token_budget":100000000,"window_seconds":60}'
export GATEWAY_API_KEY=<the raw_key it returned>
```

A generous `token_budget` matters: too small and the rate limiter's 429s
dominate the results, so you'd be measuring the limiter, not the cache.

## What the traffic mix is

`locustfile.py` sends three request shapes so both cache tiers - and a
guaranteed-miss control - are all exercised in the same run:

- **60% exact repeats** from a small fixed prompt pool -> L1 (Redis, exact
  hash) hits after warm-up.
- **20% light paraphrases** of the same pool -> may or may not clear the
  L2 semantic-similarity threshold, depending on how close the rewording
  lands.
- **20% fully unique prompts** (UUID-suffixed) -> guaranteed misses,
  giving a floor for "uncacheable" traffic cost in both runs.

`x-cache: no-store` (sent when `NO_CACHE=1`, i.e. `make load-baseline`) is
a real gateway feature (`RequestOrchestrator(..., bypass_cache=...)`), not
a locust-side trick - it skips both the cache read and the write-back, so
the baseline run neither serves from nor pollutes the cache.

## Two honest caveats

- **L1 will carry most of the hit-rate at the default 0.97 similarity
  threshold.** The 60% exact-repeat traffic reliably hits L1; the
  paraphrase traffic may legitimately fall *below* 0.97 and miss L2 - a
  conservative threshold behaving correctly, not a bug. To demonstrate L2
  specifically, temporarily lower `CACHE_SIMILARITY_THRESHOLD` (~0.90) for
  one run and say so; report the headline number at the real default.
- **Warm-up.** The first few seconds of a cache-on run are misses while
  the pool warms. A 2-minute window (the default) makes that noise
  negligible; use `DURATION=3m` for a cleaner number.
