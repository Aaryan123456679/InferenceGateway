# LLM Inference Gateway — HLD + LLD

**Project 2 (build order).** An OpenAI-compatible proxy over N heterogeneous model backends with semantic caching, policy routing, per-backend circuit breakers, and token-budget rate limiting.

**Library dependency:** consumes `aikit`. Requires an interface change → **`aikit` v0.2.0** (see §B.0). Reuses `ModelClient` (Ollama + new `HostedClient`), `EmbeddingService` (semantic cache), `observability`, `config`, `db`. **Does not** use `jobqueue` — the gateway is a synchronous request path, not a fan-out worker system. (Contrast with eval-platform is itself a talking point: two services, two concurrency models, one shared core.)

---

# PART A — HIGH-LEVEL DESIGN

## A.1 Problem
LLM traffic is routed naively: a fixed model per call, no cost or latency awareness, no failover when a backend degrades, no dedup of identical or near-identical prompts, no budget enforcement. The gateway turns a set of backends into a managed pool behind one endpoint.

## A.2 Goals / non-goals
**Goals**
- OpenAI-compatible `POST /v1/chat/completions`, streaming (SSE) and non-streaming.
- N backends (Ollama local + one hosted) behind `aikit.ModelClient`; config in DB, hot-reloadable.
- **Two-tier semantic cache**: L1 exact-hash (Redis), L2 embedding-similarity (pgvector).
- Pluggable **routing policy**: latency-aware, cost/complexity-tiered.
- Per-backend **circuit breaker** + automatic **failover**.
- **Token-budget** rate limiting per API key (reserve → reconcile).
- Full observability; stateless → horizontally scalable.

**Non-goals (v1)** — scoped deliberately.
- Multi-tenant billing, admin UI beyond metrics, streaming *responses* served from cache (v1 caches full text and replays), learned/ML router, distributed multi-region cache, cross-provider cost optimization beyond a length heuristic.

## A.3 Architecture
```
 client (OpenAI SDK)
     │  POST /v1/chat/completions  (X-API-Key)
     ▼
 ┌────────────────────────── Gateway (FastAPI, stateless) ──────────────────────┐
 │ auth → rate-limit reserve → L1 exact cache → embed → L2 semantic cache        │
 │                                   │ miss                                      │
 │                                   ▼                                           │
 │   ┌──────────────┐  candidates  ┌───────────────┐  guard  ┌───────────────┐   │
 │   │ RoutingPolicy│─────────────▶│ CircuitBreaker │────────▶│  ModelClient  │   │
 │   │ (latency/cost)│             │  per backend  │ failover │ Ollama/Hosted │   │
 │   └──────────────┘              └───────────────┘         └───────┬───────┘   │
 │        ▲ p95 stats                                                │ stream    │
 │        │                                                          ▼           │
 │   on success: record latency, breaker=success, cache put (L1+L2),            │
 │   rate-limit reconcile, request_log                                          │
 └───────┬───────────────────────────────────────────────────┬──────────────────┘
         ▼                                                    ▼
   ┌──────────────┐                                    ┌──────────────┐
   │    Redis     │ L1 cache, breaker state,           │  Postgres    │ api_key,
   │              │ rate-limit buckets, latency windows │ +pgvector    │ backend,
   └──────────────┘                                    │ L2 cache, log│ request_log
                                                       └──────────────┘
```

## A.4 Why this storage split
- **Redis** — ephemeral, atomic, hot: L1 exact cache, circuit-breaker state, rate-limit buckets, latency windows. Shared across instances.
- **pgvector** — the L2 semantic cache (embedding + response + ttl, HNSW index, cosine `<=>`) and durable config/logs. Reuses infra already proven in eval-platform. Shared across instances → horizontal scale holds.

The two-tier cache is the wow factor: identical prompts hit L1 in sub-millisecond Redis; near-duplicates hit L2 via vector similarity. Most systems only do exact-match caching.

## A.5 Stack (all $0 locally)
FastAPI + uvicorn, Redis (Upstash free in prod), Postgres + pgvector (Supabase free), Ollama local + one hosted free-tier model, `aikit` clients + embeddings, Docker. **No queue/workers.**

## A.6 Scaling & failure model
- **Stateless gateway** (all shared state in Redis/pg) → run N replicas behind a LB.
- **Cache** cuts backend load; **breaker** fails fast on degraded backends; **failover** walks the candidate list by priority.
- **Backpressure**: per-backend `asyncio.Semaphore` + per-request timeout.
- **Graceful degradation**: all backends open → 503 with `Retry-After`; embedding down → skip L2, still serve L1 + route (same lazy-resource lesson from eval-platform).

## A.7 Correctness note (semantic cache is a quality risk)
Serving a *similar* prompt's cached answer can return a subtly wrong response. Mitigations, all in v1: a conservative default threshold (≥0.97), cache namespaced by **requested model** (never serve model A's answer for model B), and a per-request opt-out (`x-cache: no-store`). Named honestly in the README.

---

# PART B — LOW-LEVEL DESIGN

## B.0 Required `aikit` change → v0.2.0 (do this first)
The stream contract must carry terminal usage so the gateway can log tokens/cost/finish-reason and write cache after a stream:
```python
# aikit.model_client
@dataclass(frozen=True)
class Usage:
    tokens_in: int
    tokens_out: int
    finish_reason: str

StreamEvent = str | Usage          # text deltas, then exactly one terminal Usage

class ModelClient(Protocol):
    async def stream(self, messages, *, model=None) -> AsyncIterator[StreamEvent]: ...
```
- Additive to `complete()`; only `stream()`'s yield type changes.
- **eval-platform stays pinned at v0.1.0** (never calls `stream()`), untouched. Gateway pins `aikit @ ...@v0.2.0`.
- **Validate live against real Ollama before building on it** — closes the one open coverage gap and de-risks the hot path. Add a `HostedClient` (OpenAI-compatible httpx) implementing the same Protocol.
- Ship with a regression test asserting exactly one terminal `Usage` per stream.

## B.1 Repository layout
```
inference-gateway/
  pyproject.toml                 # aikit@v0.2.0 (git), fastapi, uvicorn, redis, sqlalchemy, httpx
  src/gateway/
    config.py                    # GatewaySettings(BaseServiceSettings)
    domain/                      # frozen dataclasses: ApiKey, Backend, CacheEntry, RequestLog
    persistence/
      models.py repositories.py migrations/   # backend/api_key/request_log/l2_cache
    backends/
      registry.py                # loads backend rows → ModelClient instances (hot reload)
    cache/
      exact.py                   # L1 Redis exact-hash
      semantic.py                # L2 pgvector similarity
      cache.py                   # TwoTierCache (orchestrates L1→L2)
    routing/
      base.py                    # RoutingPolicy Protocol
      latency.py cost.py         # policies
    resilience/
      breaker.py                 # Redis+Lua circuit breaker
      ratelimit.py               # Redis token-budget limiter
      latency_tracker.py         # rolling p95 per backend
    api/
      schemas.py                 # OpenAI-compatible request/response
      routers.py deps.py
    orchestrator.py              # ties the request pipeline together
    main.py
  tests/
  docker/{Dockerfile,docker-compose.yml}
  .github/workflows/ci.yml
  Makefile                       # make smoke  (compose→migrate→pull model→real request→assert)
```

## B.2 Data model & DDL
```sql
CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE api_key (
  id                 uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  key_hash           char(64) NOT NULL UNIQUE,      -- sha256 of raw key
  name               text NOT NULL,
  token_budget       integer NOT NULL CHECK (token_budget > 0),
  window_seconds     integer NOT NULL DEFAULT 60 CHECK (window_seconds > 0),
  enabled            boolean NOT NULL DEFAULT true,
  created_at         timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE backend (
  id                 uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  name               text NOT NULL UNIQUE,
  type               text NOT NULL CHECK (type IN ('ollama','hosted')),
  endpoint           text NOT NULL,
  model              text NOT NULL,
  priority           integer NOT NULL DEFAULT 100,  -- lower = preferred on failover
  enabled            boolean NOT NULL DEFAULT true,
  cost_per_1k_in     numeric(10,6) NOT NULL DEFAULT 0,
  cost_per_1k_out    numeric(10,6) NOT NULL DEFAULT 0,
  created_at         timestamptz NOT NULL DEFAULT now()
);

-- L2 semantic cache
CREATE TABLE cache_entry (
  id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  model        text NOT NULL,                        -- namespacing: never cross-serve models
  prompt_hash  char(64) NOT NULL,                    -- also the L1 key
  embedding    vector(384) NOT NULL,                 -- MiniLM dim
  response     text NOT NULL,
  tokens_in    integer NOT NULL,
  tokens_out   integer NOT NULL,
  created_at   timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ix_cache_vec ON cache_entry
  USING hnsw (embedding vector_cosine_ops);
CREATE INDEX ix_cache_model_time ON cache_entry (model, created_at DESC);

CREATE TABLE request_log (
  id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  api_key_id     uuid NOT NULL REFERENCES api_key(id),
  backend_id     uuid REFERENCES backend(id),        -- null on cache hit
  cache_tier     text CHECK (cache_tier IN ('l1','l2')),  -- null on miss
  model          text NOT NULL,
  tokens_in      integer NOT NULL DEFAULT 0,
  tokens_out     integer NOT NULL DEFAULT 0,
  cost           numeric(12,6) NOT NULL DEFAULT 0,
  latency_ms     integer NOT NULL,
  status         integer NOT NULL,
  finish_reason  text,
  ts             timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ix_log_ts ON request_log (ts DESC);
-- roadmap: partition request_log by day under load
```

**Redis keyspace**
```
cache:{model}:{prompt_hash}     -> response JSON        (L1, TTL = cache_ttl)
breaker:{backend_id}            -> hash {state,failures,opened_at}
ratelimit:{api_key_id}          -> sorted-set sliding window of token costs
latency:{backend_id}            -> capped list of recent latency_ms (p95 source)
```

## B.3 Core interfaces (SOLID)
```python
# routing/base.py
@dataclass(frozen=True)
class RequestFeatures:
    model: str
    est_tokens_in: int
    complexity: float          # cheap heuristic in v1 (length-based)

class RoutingPolicy(Protocol):                 # OCP: add a policy without touching orchestrator
    name: str
    def select(self, candidates: list[Backend], f: RequestFeatures) -> Backend: ...

# resilience/breaker.py
class CircuitBreaker:                           # SRP; one instance per backend, Redis-backed
    async def allow(self, backend_id: str) -> bool: ...          # closed/half-open → True
    async def record(self, backend_id: str, ok: bool) -> None: ...# atomic via Lua

# cache/cache.py
class TwoTierCache:                             # SRP: L1 then L2
    def __init__(self, l1: ExactCache, l2: SemanticCache, embeddings: EmbeddingService): ...
    async def get(self, model: str, messages) -> CacheHit | None: ...
    async def put(self, model, messages, response, usage) -> None: ...

# resilience/ratelimit.py
class RateLimiter:
    async def reserve(self, key: ApiKey, est_tokens: int) -> bool: ...   # deny if over budget
    async def reconcile(self, key: ApiKey, actual_tokens: int) -> None: ...
```
- **DIP/LSP:** orchestrator depends on `ModelClient`, `RoutingPolicy`, cache/breaker/limiter abstractions — never concretes. `OllamaClient`/`HostedClient` are substitutable.
- **ISP:** policies expose only `select`; streaming vs non-streaming handled at the client boundary, not forced into one fat method.
- **SRP:** cache, breaker, limiter, latency tracker, registry, orchestrator each own one concern.

## B.4 Request pipeline
```
handle(request):
  key    = auth(X-API-Key)                        # api_key lookup (cached)
  feats  = extract_features(request)              # model, est tokens, complexity
  if not ratelimit.reserve(key, feats.est_tokens_in): → 429 Retry-After

  hit = cache.get(model, messages)                # L1 exact → else embed → L2 semantic (≥threshold)
  if hit: log(cache_tier); ratelimit.reconcile(key, hit.tokens); → return/replay

  candidates = registry.enabled(model) filtered by breaker.allow(), sorted by policy.select order
  for backend in candidates:                      # failover walk
      if not breaker.allow(backend): continue
      try:
          async with sem[backend], timeout(cfg.request_timeout):
              resp, usage = await call(backend, messages, stream?)   # ModelClient
          breaker.record(backend, ok=True); latency.record(backend, ms)
          cache.put(model, messages, resp, usage); ratelimit.reconcile(key, usage.total)
          log(backend, usage, ms); → return/stream
      except (Timeout, BackendError):
          breaker.record(backend, ok=False); continue                # → next candidate
  → 503 all backends unavailable
```
**Streaming:** chunks pass through as they arrive (SSE); the orchestrator accumulates text and reads the terminal `Usage` (from the v0.2.0 contract) to drive `cache.put`, `reconcile`, and `log` *after* the stream closes. v1 caches full text; a cached stream is replayed as chunks.

## B.5 Circuit breaker (atomic, multi-instance)
State machine `closed → open → half-open → closed`, transitions executed in a **Redis Lua script** so concurrent gateway instances can't corrupt shared state:
- **closed**: pass; failure → `failures++`; `failures ≥ threshold` → **open** (`opened_at=now`).
- **open**: reject fast (drives failover); after `cooldown` → **half-open**.
- **half-open**: allow one probe; success → **closed** (reset); failure → **open** (reset `opened_at`).
Lua guarantees the check-and-transition is a single atomic op — the moral equivalent of eval-platform's CAS finalize, applied to breaker state.

## B.6 Rate limiter (token budget, not request count)
Sliding-window over **tokens** per key. `reserve(est_tokens_in)` adds a provisional entry atomically and denies if the window sum exceeds `token_budget`; `reconcile(actual_in+out)` corrects the provisional to the true cost after completion. Redis sorted-set + Lua for atomic window trim + sum.

## B.7 Routing policies
- **LatencyAwarePolicy** — rank healthy candidates by rolling **p95** (from `latency:{backend}`), pick lowest. EWMA fallback when the window is cold.
- **CostTieredPolicy** — route by `complexity`: short/simple prompts → cheap local model, long/complex → larger/hosted, using `cost_per_1k_*`. v1 complexity is a length/token heuristic; roadmap = learned router. New policies are new classes (OCP).

## B.8 API surface
```
POST /v1/chat/completions     OpenAI-compatible; `stream: true` → SSE. X-API-Key required.
GET  /v1/models               lists enabled backends' models
POST /admin/backends          upsert backend config (admin key)     → hot reload
POST /admin/keys              create api key (admin key)
GET  /health  /ready  /metrics
```

## B.9 Config
```python
class GatewaySettings(BaseServiceSettings):
    admin_key: str
    cache_similarity_threshold: float = 0.97
    cache_ttl_seconds: int = 3600
    breaker_failure_threshold: int = 5
    breaker_cooldown_seconds: int = 30
    request_timeout_seconds: float = 30.0
    per_backend_concurrency: int = 16
    default_policy: str = "latency"
```
Env: `DATABASE_URL, REDIS_URL, ADMIN_KEY, OLLAMA_HOST, HOSTED_API_BASE, HOSTED_API_KEY, LOG_LEVEL`.

## B.10 Observability
`gateway_requests_total{status,cache}`, `gateway_cache_hits_total{tier}`, `gateway_backend_calls_total{backend,outcome}`, `gateway_breaker_state{backend}` (gauge), `gateway_request_latency_seconds{cache}` (histogram), `gateway_tokens_total{backend,direction}`, `gateway_failover_total`, `gateway_rate_limited_total`. Structured logs correlated by request id. `/ready` verifies Postgres + Redis.

## B.11 Testing strategy (mirror eval-platform rigor)
- **Unit**: each routing policy; breaker (every transition + Lua atomicity under simulated concurrency); rate limiter (reserve/deny/reconcile, window rollover); TwoTierCache (L1 hit, L2 threshold boundary, model namespacing, TTL expiry); complexity heuristic.
- **Integration in Docker** (real Redis + pgvector + real Ollama): full request path; repeat request → L1 hit; paraphrase → L2 hit; **kill a backend → failover**; N failures → **breaker opens then recovers** via half-open; over-budget key → 429; **streaming passthrough carries a terminal Usage** (the v0.2.0 contract validated end-to-end).
- **`make smoke`** from day one: cold `compose up --build` → migrate → pull model → one real streamed request → assert `200` + cache-miss then a second identical request asserts L1 hit. Reproducible cold, like AgentEval.

## B.12 Deployment
Single-service multi-stage Dockerfile (no worker target). compose: `postgres:pgvector`, `redis`, `ollama`, `gateway`. Stateless → scale replicas on Fly.io/Railway; Supabase Postgres; Upstash Redis. `/health`+`/ready` wired to platform checks.

## B.13 Carry-over lessons (baked in from the eval-platform build)
1. **CI installs real extras**, not just `dev` — type-check/test concrete impls (redis, pgvector, httpx paths), not just Protocols.
2. `tests/fakes` importable under plain `pytest`; `conftest.py` falls back to `DATABASE_URL`/`REDIS_URL` env, no hardcoded ports.
3. Docker `context:` resolves relative to the **compose file's directory** — set it deliberately if the repo nests.
4. Pin the **CPU torch wheel index** in the Dockerfile (embeddings pulls torch); no CUDA in a CPU container.
5. **Lazy resource init** — never block FastAPI startup on model/HF downloads (same pattern as the lazy ScorerFactory); embeddings resolve on first L2 use.
6. Direct-URL dep (`aikit @ git+...`) **re-resolves on every install** — fix install order (aikit first) consistently across Makefile, CI, Dockerfile; add `git` to the base image.
7. `smoke.sh` retries: use `if cmd; then break; fi`, not `cmd && break`, under `set -e`.
8. No queue here, but the arq `__qualname__` lesson generalizes: verify framework dispatch/registration with one real end-to-end call, never trust unit doubles alone.
9. Commit per change; `make smoke` green (cold) is the gate before tagging anything.

## B.14 Build milestones (each ends in a conventional commit)
1. `feat(aikit): add Usage/StreamEvent to stream contract` → tag `aikit v0.2.0`
2. `feat(aikit): HostedClient (OpenAI-compatible) + live stream test`
3. `chore: scaffold inference-gateway on aikit@v0.2.0`
4. `feat: data model + Alembic baseline (api_key, backend, cache_entry, request_log)`
5. `feat: backend registry with hot-reloadable config`
6. `feat: L1 exact cache (Redis)`
7. `feat: L2 semantic cache (pgvector) + TwoTierCache`
8. `feat: circuit breaker with atomic Lua transitions`
9. `feat: token-budget rate limiter (reserve/reconcile)`
10. `feat: latency tracker + routing policies (latency, cost)`
11. `feat: request orchestrator with failover`
12. `feat: OpenAI-compatible API incl. SSE streaming`
13. `feat: observability (metrics/health/ready) + structured logs`
14. `build: Dockerfile + compose + make smoke`
15. `test: live integration — cache tiers, failover, breaker, rate limit, streaming`
16. `docs: README with architecture, verified/known-gaps/roadmap`
