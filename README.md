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
- **Load testing, two separate questions, two separate tests** — raw CSVs
  and a script that derives every number below from them (not hand-typed)
  are committed at [`loadtest/results/`](loadtest/results/); regenerate
  with `python3 loadtest/summarize.py`.

  **1. Does the cache actually reduce latency?** `make load` /
  `make load-baseline` run the *same* 3-concurrent-user, 10-minute traffic
  mix twice against real Postgres/Redis/Ollama - once with the cache
  bypassed (`x-cache: no-store`, a real orchestrator control, not a
  client-side trick) as the baseline, once with it live. The mix is 60%
  exact repeats from a 5-prompt pool, 20% light paraphrases of that same
  pool, 20% fully-unique misses (`loadtest/locustfile.py`) - i.e. a
  workload with substantial key overlap by design, because that's what
  the L1/L2 split is *for*; a workload with no repeated or near-duplicate
  prompts would show no cache benefit almost by definition, and that's not
  a hidden caveat, it's the point of the test. Both runs use
  `loadtest-model`, a `qwen2.5:0.5b` derivative with `num_predict` capped
  at 60 tokens - **both baseline and cache-on run under this same cap**,
  so the delta is what's real; the absolute 5-16s baseline latencies are
  an artifact of a small model on this sandbox's CPU, not a claim about
  production latency anywhere else.

  | | requests | p95 | median | hit-rate |
  |---|---|---|---|---|
  | cache off (baseline) | 262 | 16.0s | 5.0s | 0% |
  | cache on | 1,695 | 5.3s | 70ms | 88.2% |

  **~67% p95 reduction, ~99% median reduction, 6.5x more requests served
  in the same window** (cache hits free up capacity for more traffic) -
  under this specific, disclosed, repeat-heavy workload.

  **2. Was the *gateway* ever tested under real concurrency, or only the
  cache path?** Fair question - the 3-user ceiling above was Ollama's, not
  the gateway's, and that distinction needs its own evidence, not just an
  assertion. `loadtest/stub_backend.py` is a stub Ollama server that
  answers instantly; pointing the gateway at it isolates the gateway's own
  pipeline (auth, rate-limit reserve/reconcile, cache read/write, routing,
  breaker checks, request logging) from real inference latency. At 50
  concurrent users, `bypass_cache=true` (so every request still does the
  full non-cached pipeline, not a cheap L1 short-circuit), 2 minutes: the
  gateway served **13,215 requests, 0 failures, p95 560ms, median 48ms,
  ~111 req/s**. That's the real concurrency number - the model server was
  the bottleneck in test 1, not the gateway.

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
9. **The gateway's own DB connection pool, not `per_backend_concurrency`,
   was the real ceiling.** The very first 50-user run against the instant
   stub backend (built to *prove* the gateway wasn't the bottleneck)
   failed 44% of requests on `QueuePool limit of size 5 overflow 10
   reached` - every request logs to Postgres, and SQLAlchemy's default
   pool (15 connections total) is far below 50 concurrent in-flight
   writes. Fixed by exposing `pool_size`/`max_overflow` on `aikit`'s
   `make_engine()` (defaults unchanged, so existing pinned consumers see
   no behavior change) and sizing the gateway's own `GatewaySettings` above
   its expected concurrency. The 13,215-request, 0-failure result above is
   *after* this fix - the number this project would have reported without
   digging into the 500s was a worse, and wrong, one.

**Known gaps**:
- Circuit breaker open→half-open→closed *timing* and L2 paraphrase-
  similarity are proven by the (stronger, deterministic) test suite, not
  by the live smoke run — reproducing them reliably against a live model
  in a fast smoke script would be flaky.
- `HostedClient` (OpenAI-compatible backends) has no live-Docker
  validation — only aikit's own mock-tested wire-format coverage. No real
  hosted API key was available in this environment.
- The cache A/B test itself still used 3 concurrent users, not 50 - real
  Ollama on this sandbox's CPU couldn't sustain more (bug/fix #7 above was
  found trying). That ceiling is now *demonstrated* to be the model
  server's, not the gateway's, by the separate 50-user stub-backend test -
  but the two tests are still separate runs, not one 50-user run against a
  real model with the cache live. That combined number is the natural next
  load test, not yet run.
- Admin endpoints have no audit log beyond `request_log`'s own rows.
