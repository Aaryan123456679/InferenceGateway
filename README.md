# inference-gateway

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
- 43 tests against real Postgres+pgvector and real Redis (fakeredis has no
  EVAL/EVALSHA support - the breaker and rate limiter are both Lua-
  scripted and need the real thing): routing policies, cache tiers (L1
  short-circuit, L2 threshold boundary and model namespacing via a
  `FakeEmbeddingService` with pinned vectors), circuit breaker state
  transitions, rate limiter reserve/deny/reconcile/window-rollover,
  backend registry hot-reload, and full-pipeline orchestrator tests
  (happy path, cache hit, 429, failover, all-backends-down, breaker trip,
  and the streaming partial-output-must-not-silently-failover guarantee).
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

**Known gaps**:
- Circuit breaker open→half-open→closed *timing* and L2 paraphrase-
  similarity are proven by the (stronger, deterministic) test suite, not
  by the live smoke run — reproducing them reliably against a live model
  in a fast smoke script would be flaky.
- `HostedClient` (OpenAI-compatible backends) has no live-Docker
  validation — only aikit's own mock-tested wire-format coverage. No real
  hosted API key was available in this environment.
- No load/concurrency testing beyond the breaker's unit-level race
  assertion — replica-level behavior under many simultaneous requests is
  unmeasured.
- Admin endpoints have no audit log beyond `request_log`'s own rows.
