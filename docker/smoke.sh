#!/usr/bin/env bash
# Real end-to-end proof: real Postgres, real Redis, real Ollama, all in
# Docker. Scope note: circuit-breaker open->half-open->closed timing and
# L2 semantic-cache paraphrase similarity are proven by the (stronger,
# deterministic) integration test suite against real Postgres/Redis with a
# controlled FakeEmbeddingService - reproducing paraphrase similarity
# reliably against a live model in a fast smoke run would be flaky. This
# script proves the wiring: real chat completion, L1 cache hit, real SSE
# streaming with terminal usage, real failover against a genuinely dead
# backend, and real rate-limit enforcement.
set -euo pipefail
cd "$(dirname "$0")/.."

COMPOSE="docker compose -f docker/docker-compose.yml"
source .env

echo "==> building images"
$COMPOSE up -d --build

echo "==> waiting for postgres/redis to report healthy"
for i in $(seq 1 30); do
  status=$($COMPOSE ps --format json postgres redis | python3 -c "
import sys, json
rows = [json.loads(l) for l in sys.stdin if l.strip()]
print(all(r.get('Health') == 'healthy' for r in rows))
")
  if [ "$status" = "True" ]; then
    break
  fi
  sleep 2
done

echo "==> running migration inside the gateway container"
$COMPOSE exec -T gateway alembic upgrade head

echo "==> waiting for gateway /health"
for i in $(seq 1 30); do
  if curl -sf http://localhost:8001/health >/dev/null; then
    break
  fi
  sleep 2
done
curl -sf http://localhost:8001/health

echo "==> pulling ${DEFAULT_MODEL} into the ollama container"
$COMPOSE exec -T ollama ollama pull "${DEFAULT_MODEL}"

BASE=http://localhost:8001

echo "==> creating an admin-issued API key"
KEY_JSON=$(curl -sf -X POST "$BASE/admin/keys" \
  -H "X-Admin-Key: ${ADMIN_KEY}" -H "Content-Type: application/json" \
  -d '{"name": "smoke", "token_budget": 100000, "window_seconds": 60}')
API_KEY=$(echo "$KEY_JSON" | python3 -c "import sys,json; print(json.load(sys.stdin)['raw_key'])")
echo "api_key acquired"

echo "==> registering a healthy backend"
curl -sf -X POST "$BASE/admin/backends" \
  -H "X-Admin-Key: ${ADMIN_KEY}" -H "Content-Type: application/json" \
  -d '{"name": "healthy", "type": "ollama", "endpoint": "http://ollama:11434", "model": "'"${DEFAULT_MODEL}"'", "priority": 100}' \
  >/dev/null

echo "==> real (non-streaming) chat completion"
RESP1=$(curl -sf -X POST "$BASE/v1/chat/completions" \
  -H "X-API-Key: ${API_KEY}" -H "Content-Type: application/json" \
  -d '{"model": "'"${DEFAULT_MODEL}"'", "messages": [{"role": "user", "content": "Say hi in one short sentence."}]}')
echo "$RESP1" | python3 -c "
import sys, json
data = json.load(sys.stdin)
assert data['choices'][0]['message']['content'], 'empty completion'
assert data['cache_tier'] is None, f\"expected a fresh (uncached) response, got {data['cache_tier']}\"
assert data['usage']['total_tokens'] > 0
print('first request: cache_tier=None, content=', repr(data['choices'][0]['message']['content'][:40]))
"

echo "==> repeat request must hit the L1 cache"
RESP2=$(curl -sf -X POST "$BASE/v1/chat/completions" \
  -H "X-API-Key: ${API_KEY}" -H "Content-Type: application/json" \
  -d '{"model": "'"${DEFAULT_MODEL}"'", "messages": [{"role": "user", "content": "Say hi in one short sentence."}]}')
echo "$RESP2" | python3 -c "
import sys, json
data = json.load(sys.stdin)
assert data['cache_tier'] == 'l1', f\"expected L1 cache hit, got {data['cache_tier']}\"
print('second request: cache_tier=l1 (confirmed)')
"

echo "==> streaming request must carry a terminal usage chunk"
STREAM_OUT=$(curl -sf -N -X POST "$BASE/v1/chat/completions" \
  -H "X-API-Key: ${API_KEY}" -H "Content-Type: application/json" \
  -d '{"model": "'"${DEFAULT_MODEL}"'", "messages": [{"role": "user", "content": "Count to three."}], "stream": true}')
echo "$STREAM_OUT" | python3 -c "
import sys, json
lines = [l[len('data: '):] for l in sys.stdin.read().splitlines() if l.startswith('data: ')]
assert lines[-1] == '[DONE]', 'stream did not end with [DONE]'
chunks = [json.loads(l) for l in lines[:-1]]
usage_chunks = [c for c in chunks if c.get('usage')]
assert len(usage_chunks) == 1, f'expected exactly one terminal usage chunk, got {len(usage_chunks)}'
assert usage_chunks[0]['usage']['total_tokens'] > 0
assert usage_chunks[-1] is chunks[-1], 'usage chunk must be last'
print('stream OK: terminal usage =', usage_chunks[0]['usage'])
"

echo "==> registering a broken backend ahead of the healthy one (failover proof)"
curl -sf -X POST "$BASE/admin/backends" \
  -H "X-Admin-Key: ${ADMIN_KEY}" -H "Content-Type: application/json" \
  -d '{"name": "broken", "type": "ollama", "endpoint": "http://ollama-does-not-exist:11434", "model": "'"${DEFAULT_MODEL}"'", "priority": 1}' \
  >/dev/null

RESP3=$(curl -sf -X POST "$BASE/v1/chat/completions" \
  -H "X-API-Key: ${API_KEY}" -H "Content-Type: application/json" \
  -d '{"model": "'"${DEFAULT_MODEL}"'", "messages": [{"role": "user", "content": "Say bye in one short sentence."}]}')
echo "$RESP3" | python3 -c "
import sys, json
data = json.load(sys.stdin)
assert data['choices'][0]['message']['content'], 'empty completion after failover'
print('failover OK: request succeeded despite a dead higher-priority backend')
"

echo "==> over-budget key must be rate limited (429)"
LOW_KEY_JSON=$(curl -sf -X POST "$BASE/admin/keys" \
  -H "X-Admin-Key: ${ADMIN_KEY}" -H "Content-Type: application/json" \
  -d '{"name": "smoke-low-budget", "token_budget": 1, "window_seconds": 60}')
LOW_KEY=$(echo "$LOW_KEY_JSON" | python3 -c "import sys,json; print(json.load(sys.stdin)['raw_key'])")
STATUS=$(curl -s -o /dev/null -w "%{http_code}" -X POST "$BASE/v1/chat/completions" \
  -H "X-API-Key: ${LOW_KEY}" -H "Content-Type: application/json" \
  -d '{"model": "'"${DEFAULT_MODEL}"'", "messages": [{"role": "user", "content": "This prompt is long enough to exceed a 1-token budget."}]}')
if [ "$STATUS" != "429" ]; then
  echo "expected 429, got $STATUS" >&2
  exit 1
fi
echo "rate limit OK: got 429 as expected"

echo "==> SMOKE TEST PASSED"
