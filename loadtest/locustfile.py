"""Gateway load test.

Two runs give the causal number:
  make load            # cache ON
  make load-baseline   # cache OFF (sends `x-cache: no-store`)
Compare aggregated p95 between the two -> that delta is the cache's effect.

Env:
  GATEWAY_API_KEY  required - a key with a GENEROUS token_budget, else the
                   rate limiter (429s) dominates and you measure the limiter,
                   not the cache.
  GATEWAY_MODEL    default qwen2.5:0.5b
  NO_CACHE         set to bypass the cache (baseline run)
"""
import os
import random
import uuid

from locust import HttpUser, between, task

API_KEY = os.environ["GATEWAY_API_KEY"]
MODEL = os.environ.get("GATEWAY_MODEL", "qwen2.5:0.5b")
NO_CACHE = os.environ.get("NO_CACHE", "") not in ("", "0", "false")

# Small fixed pool -> identical repeats exercise the L1 exact cache after warmup.
POOL = [
    "Explain what a mutex is in one sentence.",
    "What is the capital of France?",
    "Summarize the CAP theorem briefly.",
    "Define idempotency for an HTTP API.",
    "What does ACID stand for in databases?",
]
# Light rewordings -> near-duplicates exercise the L2 semantic cache.
PARAPHRASE = ["Please ", "Can you ", "I'd like to know: ", "Quick question - "]


def _headers():
    h = {"X-API-Key": API_KEY, "Content-Type": "application/json"}
    if NO_CACHE:
        h["x-cache"] = "no-store"
    return h


def _body(prompt):
    return {"model": MODEL, "stream": False,
            "messages": [{"role": "user", "content": prompt}]}


class GatewayUser(HttpUser):
    wait_time = between(0.1, 0.5)

    @task(6)  # ~60% identical repeats -> L1 hits
    def exact(self):
        self.client.post("/v1/chat/completions", json=_body(random.choice(POOL)),
                         headers=_headers(), name="exact")

    @task(2)  # ~20% near-duplicates -> L2 semantic hits (threshold-dependent)
    def semantic(self):
        p = random.choice(PARAPHRASE) + random.choice(POOL).lower()
        self.client.post("/v1/chat/completions", json=_body(p),
                         headers=_headers(), name="semantic")

    @task(2)  # ~20% unique -> guaranteed misses (baseline cost + routing path)
    def miss(self):
        p = f"Give me fact number {uuid.uuid4()} about distributed systems."
        self.client.post("/v1/chat/completions", json=_body(p),
                         headers=_headers(), name="miss")
