# Gateway load test

Two `make` commands give the causal number: how much of the gateway's
latency the two-tier cache actually removes, under the same traffic.

```bash
make load-baseline   # cache OFF (x-cache: no-store) - the control
make load             # cache ON
```

Each prints an aggregated p95 (ms) and a cache hit-rate over its own
window. The reduction is `(p95_baseline - p95_cache) / p95_baseline`.

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
