from __future__ import annotations

import hashlib
import json
import secrets
import time
import uuid
from collections.abc import AsyncIterator

from aikit.db import session_scope
from aikit.model_client import ChatMessage
from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from fastapi.responses import StreamingResponse
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from gateway.api.deps import (
    get_backend_repo,
    get_orchestrator,
    get_session,
    require_admin_key,
    require_api_key,
)
from gateway.api.schemas import (
    ApiKeyCreateRequest,
    ApiKeyCreateResponse,
    BackendOut,
    BackendUpsertRequest,
    ChatCompletionChoice,
    ChatCompletionChunk,
    ChatCompletionChunkChoice,
    ChatCompletionChunkDelta,
    ChatCompletionMessageOut,
    ChatCompletionRequest,
    ChatCompletionResponse,
    ChatCompletionUsage,
    ModelInfo,
    ModelList,
)
from gateway.domain import ApiKey, BackendType
from gateway.orchestrator import (
    AllBackendsUnavailable,
    NoBackendsAvailable,
    RateLimitExceeded,
    RequestOrchestrator,
)
from gateway.persistence.repositories import ApiKeyRepo, BackendRepo

router = APIRouter()
admin_router = APIRouter(prefix="/admin", dependencies=[Depends(require_admin_key)])
system_router = APIRouter()


def _to_chat_messages(body: ChatCompletionRequest) -> list[ChatMessage]:
    return [ChatMessage(role=m.role, content=m.content) for m in body.messages]


@router.post("/v1/chat/completions", response_model=None)
async def chat_completions(
    body: ChatCompletionRequest,
    request: Request,
    api_key: ApiKey = Depends(require_api_key),
    orchestrator: RequestOrchestrator = Depends(get_orchestrator),
) -> ChatCompletionResponse | StreamingResponse:
    messages = _to_chat_messages(body)
    completion_id = f"chatcmpl-{uuid.uuid4().hex}"
    created = int(time.time())
    # Load-test control (loadtest/locustfile.py's baseline run): lets an A/B
    # comparison force real cache misses on the same traffic mix used by the
    # cache-on run, without a separate deployment or config flag.
    bypass_cache = request.headers.get("x-cache") == "no-store"

    if body.stream:

        async def event_stream() -> AsyncIterator[str]:
            try:
                async for event in orchestrator.handle_stream(
                    api_key=api_key, model=body.model, messages=messages,
                    bypass_cache=bypass_cache,
                ):
                    if isinstance(event, str):
                        chunk = ChatCompletionChunk(
                            id=completion_id,
                            created=created,
                            model=body.model,
                            choices=[
                                ChatCompletionChunkChoice(
                                    delta=ChatCompletionChunkDelta(content=event)
                                )
                            ],
                        )
                        yield f"data: {chunk.model_dump_json()}\n\n"
                    else:
                        final = ChatCompletionChunk(
                            id=completion_id,
                            created=created,
                            model=event.model,
                            choices=[
                                ChatCompletionChunkChoice(
                                    delta=ChatCompletionChunkDelta(),
                                    finish_reason=event.finish_reason,
                                )
                            ],
                            usage=ChatCompletionUsage(
                                prompt_tokens=event.tokens_in,
                                completion_tokens=event.tokens_out,
                                total_tokens=event.tokens_in + event.tokens_out,
                            ),
                        )
                        yield f"data: {final.model_dump_json()}\n\n"
            except RateLimitExceeded:
                error = {"message": "rate limit exceeded", "type": "rate_limit"}
                yield f"data: {json.dumps({'error': error})}\n\n"
            except (NoBackendsAvailable, AllBackendsUnavailable):
                error = {"message": "no backend available", "type": "unavailable"}
                yield f"data: {json.dumps({'error': error})}\n\n"
            yield "data: [DONE]\n\n"

        return StreamingResponse(event_stream(), media_type="text/event-stream")

    try:
        result = await orchestrator.handle_complete(
            api_key=api_key, model=body.model, messages=messages, bypass_cache=bypass_cache
        )
    except RateLimitExceeded as exc:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="rate limit exceeded",
            headers={"Retry-After": "1"},
        ) from exc
    except NoBackendsAvailable as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"no backend configured for model {body.model!r}",
        ) from exc
    except AllBackendsUnavailable as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="all candidate backends failed or are circuit-open",
        ) from exc

    return ChatCompletionResponse(
        id=completion_id,
        created=created,
        model=result.model,
        choices=[
            ChatCompletionChoice(
                message=ChatCompletionMessageOut(content=result.content),
                finish_reason=result.finish_reason,
            )
        ],
        usage=ChatCompletionUsage(
            prompt_tokens=result.tokens_in,
            completion_tokens=result.tokens_out,
            total_tokens=result.tokens_in + result.tokens_out,
        ),
        cache_tier=result.cache_tier.value if result.cache_tier else None,
    )


@router.get("/v1/models")
async def list_models(repo: BackendRepo = Depends(get_backend_repo)) -> ModelList:
    backends = await repo.list_all()
    models = sorted({b.model for b in backends if b.enabled})
    return ModelList(data=[ModelInfo(id=m) for m in models])


@admin_router.post("/backends", response_model=BackendOut, status_code=status.HTTP_201_CREATED)
async def upsert_backend(body: BackendUpsertRequest, request: Request) -> BackendOut:
    # Deliberately not Depends(get_backend_repo): that dependency's
    # transaction only commits during FastAPI's post-handler cleanup, which
    # runs *after* this function returns. reload() opens its own session to
    # read the backend table, and under READ COMMITTED it can't see a row
    # that isn't committed yet - it would silently reload without the very
    # backend this call just wrote. Scoping the transaction explicitly here
    # guarantees it's committed before reload() ever runs.
    async with session_scope(request.app.state.sessionmaker) as session:
        backend = await BackendRepo(session).upsert(
            name=body.name,
            type=BackendType(body.type),
            endpoint=body.endpoint,
            model=body.model,
            priority=body.priority,
            cost_per_1k_in=body.cost_per_1k_in,
            cost_per_1k_out=body.cost_per_1k_out,
        )
    await request.app.state.registry.reload()
    return BackendOut(
        id=backend.id, name=backend.name, type=backend.type.value, endpoint=backend.endpoint,
        model=backend.model, priority=backend.priority, enabled=backend.enabled,
    )


@admin_router.post(
    "/keys", response_model=ApiKeyCreateResponse, status_code=status.HTTP_201_CREATED
)
async def create_api_key(
    body: ApiKeyCreateRequest, session: AsyncSession = Depends(get_session)
) -> ApiKeyCreateResponse:
    raw_key = f"sk-{secrets.token_urlsafe(32)}"
    key_hash = hashlib.sha256(raw_key.encode()).hexdigest()
    created = await ApiKeyRepo(session).create(
        name=body.name, key_hash=key_hash, token_budget=body.token_budget,
        window_seconds=body.window_seconds,
    )
    return ApiKeyCreateResponse(id=created.id, raw_key=raw_key)


@system_router.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@system_router.get("/ready")
async def ready(request: Request, session: AsyncSession = Depends(get_session)) -> dict[str, str]:
    await session.execute(text("SELECT 1"))
    await request.app.state.redis.ping()
    return {"status": "ready"}


@system_router.get("/metrics")
async def metrics(request: Request) -> Response:
    body = request.app.state.metrics.render()
    return Response(content=body, media_type="text/plain; version=0.0.4")
