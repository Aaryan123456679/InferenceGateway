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
