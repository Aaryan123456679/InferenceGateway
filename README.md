# inference-gateway

*Part of the [AI Infrastructure Suite](https://github.com/Aaryan123456679/ai-infrastructure-suite) — one of three services built on the shared `aikit` library.*

OpenAI-compatible proxy over N backends: two-tier semantic caching, policy
routing, per-backend circuit breakers, token-budget rate limiting. Full
design in [`HLD+LLD.md`](HLD+LLD.md).

Consumes [`aikit`](../aikit) v0.2.0 for `ModelClient` (`OllamaClient` +
`HostedClient`), `EmbeddingService`, `BaseServiceSettings`, the async DB
base, and observability helpers. Unlike `eval-platform`, this project does
**not** use `jobqueue` — it's a synchronous request path, not a fan-out
worker system.

## Develop

```bash
# Order matters: this repo's own deps first, aikit editable LAST - it's
# pinned to a git tag here, and installing it after silently re-pins it
# back to that tag instead of your local edits.
pip install -e ".[dev]"
pip install -e "../aikit[gateway]"
cp .env.example .env   # fill in ADMIN_KEY at minimum
docker compose -f docker/docker-compose.yml up -d postgres redis ollama
alembic upgrade head
pytest
```

Run it:

```bash
uvicorn gateway.main:app --reload
```

## Architecture

```
client -> POST /v1/chat/completions -> auth -> rate-limit reserve
   -> L1 exact cache (Redis) -> L2 semantic cache (pgvector, model-namespaced)
   -> RoutingPolicy over enabled, breaker-allowed backends -> ModelClient call
   -> cache write + rate-limit reconcile + request_log
```

Failover walks the candidate list on any backend failure - except mid-
stream: once a chunk has reached the client, a later failure ends the
response rather than silently splicing in a second backend's output.

See [`HLD+LLD.md`](HLD+LLD.md) for the full design (schema, breaker/rate-
limiter Lua scripts, routing policies, API surface).

## Key design decisions

- **The routing policy picks one backend at a time, not a ranking** —
  `select()` returns a single `Backend`; the orchestrator's failover loop
  calls it again on the shrinking candidate list after each failure. This
  matches the literal interface in the design doc while still producing a
  full best-to-worst walk.
- **Streaming failover stops at the first sent byte** — a real correctness
  issue, not just a style choice: once text has reached the SSE client, a
  second backend's output can't be spliced in after a partial response
  without corrupting what the client already has.
- **The circuit breaker's state transitions are Lua, not application code**
  — closed/open/half-open changes must be atomic across N gateway
  replicas sharing one Redis; the same reasoning as eval-platform's
  CAS-guarded run finalization, applied to breaker state instead of a
  Postgres row.
- **Embeddings resolve lazily, once, on first L2 use** — loading
  sentence-transformers is slow and network-bound; it has no business
  gating `/health` or blocking the whole API from starting.
- **The registry starts empty and self-heals rather than refusing to
  boot** — if migrations haven't landed yet when the container starts, the
  gateway still serves `/health`; chat completions 503 until the schema
  catches up or an admin call repopulates it.

## Non-goals (v1)

Multi-tenant billing, an admin UI beyond the REST endpoints, streaming
*responses* served from cache (v1 caches full text and replays it as
chunks), a learned/ML router, distributed multi-region cache, cost
optimization beyond a length heuristic.

## Status

**Verified**, not just written:
- 45 tests against real Postgres+pgvector and real Redis (fakeredis has no
  EVAL/EVALSHA support - the breaker and rate limiter are both Lua-
  scripted and need the real thing): routing policies, cache tiers (L1
  short-circuit, L2 threshold boundary and model namespacing via a
  `FakeEmbeddingService` with pinned vectors), circuit breaker state
  transitions, rate limiter reserve/deny/reconcile/window-rollover,
  backend registry hot-reload, full-pipeline orchestrator tests (happy
  path, cache hit, 429, failover, all-backends-down, breaker trip, the
  streaming partial-output-must-not-silently-failover guarantee), and the
  `bypass_cache` load-test control (a bypassed request must neither read
  nor write either tier).
- `make smoke`: a full cold `docker compose up --build`, Alembic migration,
  a real Ollama model pull, and against real infrastructure - a real
  non-streaming chat completion, a repeat request confirmed served from
  the L1 cache, a real SSE stream with exactly one terminal usage chunk,
  a real failover against a genuinely dead backend endpoint, and a real
  429 from the token-budget rate limiter. Reproducible cold (fresh
  volumes), not just on a warm build.
- `aikit`'s `ModelClient.stream()` contract (`str | Usage`) was live-
  validated against a real Ollama server, including a production-sized
  model (`llama3.1:8b`), before this project was built on top of it.
- `make load` / `make load-baseline` ([`loadtest/`](loadtest/)): a real
  A/B load test against a live gateway, real Postgres/Redis, and real
  Ollama - same 3-concurrent-user traffic mix (exact repeats, paraphrases,
  unique misses) run twice, once with the cache bypassed (`x-cache:
  no-store`, a real orchestrator control, not a client-side trick) as the
  control. Over a 10-minute window: baseline (cache off) served 247
  requests, p95 **17.0s**, median 4.6s, 0% hit-rate. Cache on served
  **1,980 requests in the same window** (8x the throughput - cache hits
  free up capacity for more traffic), p95 **3.5s**, median **67ms**, 91.6%
  hit-rate. That's a **~79% p95 reduction** and a **~98.5% median
  reduction** from caching alone, on identical traffic. (Run against
  `loadtest-model`, a `qwen2.5:0.5b` derivative with `num_predict` capped
  at 60 tokens - bounding generation length for reproducible latency
  samples in a load test, not a change to the production model.)

This process found six real bugs, none of them caught by the unit/
integration test suite alone:
1. FastAPI crashed at import: a route return-type annotation
   (`ChatCompletionResponse | StreamingResponse`) isn't a valid Pydantic
   field — needed `response_model=None`.
2. The gateway crashed at startup whenever it started before migrations
   were applied — fixed by catching the initial registry reload's failure
   and degrading gracefully instead of refusing to boot.
3. `POST /admin/backends` silently reloaded the registry from a stale,
   pre-commit view of its own upsert — a FastAPI dependency's transaction
   commits *after* the handler returns, so `reload()` (a separate session)
   couldn't yet see the row the same request had just written. `GET
   /v1/models` (a fresh read on a later request) saw it fine, which is
   what made this look like "the write didn't happen" rather than "the
   reload ran too early."
4. redis-py's async return-type stubs aren't stable across versions —
   neither a version-pinned `# type: ignore` nor a `cast()` survives mypy
   strict mode across both an old (imprecise) and new (precise) stub
   shape simultaneously; landed on disabling `warn_redundant_casts`
   specifically rather than chasing stub churn.
5. Docker resolved the CUDA-enabled torch wheel by default (several GB of
   unused NVIDIA packages in a CPU-only container) - needed the CPU wheel
   index, same fix as eval-platform's Dockerfile.
6. The base image had no `git`, which pip needs to clone the
   `aikit @ git+...` direct dependency.

The load test itself (added after the above) found two more, neither of
them caught by the unit/integration suite because neither is reachable
without real concurrent traffic:

7. **`aikit`'s embedding service crashed the whole process under
   concurrency.** `SentenceTransformerEmbeddingService` let `torch`
   auto-select a device, which picked MPS (Apple Silicon GPU) on this
   machine; calling `encode()` from multiple concurrent asyncio executor
   threads - exactly what happens with several in-flight requests each
   needing an L2 embedding - crashed the process outright rather than
   raising a catchable exception (1,980 requests reduced to instant
   `ConnectionRefusedError`s once it died). Fixed in `aikit` by passing
   `device="cpu"` explicitly: no interface change, so existing pinned
   consumers are unaffected, and CPU is the right choice for this
   workload's shape anyway (many small, latency-sensitive, single-item
   calls, not the large batches GPU dispatch overhead pays for).
8. **`loadtest/run.sh` hid its own report on any failure.** `set -e` plus
   locust's non-zero exit on even one failed request (out of hundreds)
   meant the summary block - the actual point of the script - silently
   never printed on exactly the runs where seeing it mattered most. Fixed
   with `|| true` on the locust invocation; the failure detail is already
   visible in locust's own printed table above it.

**Known gaps**:
- Circuit breaker open→half-open→closed *timing* and L2 paraphrase-
  similarity are proven by the (stronger, deterministic) test suite, not
  by the live smoke run — reproducing them reliably against a live model
  in a fast smoke script would be flaky.
- `HostedClient` (OpenAI-compatible backends) has no live-Docker
  validation — only aikit's own mock-tested wire-format coverage. No real
  hosted API key was available in this environment.
- The load test used 3 concurrent users, not the 50 a "real" load test
  implies - this sandbox's single CPU-only Ollama instance couldn't sustain
  more (see `loadtest/README.md`'s notes on this). The *relative* cache
  effect (p95/median/hit-rate) is real and reproducible; the *absolute*
  throughput ceiling is an artifact of this environment, not the gateway.
- Admin endpoints have no audit log beyond `request_log`'s own rows.
