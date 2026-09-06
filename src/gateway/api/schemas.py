from __future__ import annotations

from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field


class ChatMessageIn(BaseModel):
    role: Literal["system", "user", "assistant"]
    content: str


class ChatCompletionRequest(BaseModel):
    model: str
    messages: list[ChatMessageIn]
    stream: bool = False


class ChatCompletionMessageOut(BaseModel):
    role: Literal["assistant"] = "assistant"
    content: str


class ChatCompletionChoice(BaseModel):
    index: int = 0
    message: ChatCompletionMessageOut
    finish_reason: str | None


class ChatCompletionUsage(BaseModel):
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int


class ChatCompletionResponse(BaseModel):
    id: str
    object: Literal["chat.completion"] = "chat.completion"
    created: int
    model: str
    choices: list[ChatCompletionChoice]
    usage: ChatCompletionUsage
    cache_tier: str | None = None  # gateway extension: null | "l1" | "l2"


class ChatCompletionChunkDelta(BaseModel):
    content: str | None = None


class ChatCompletionChunkChoice(BaseModel):
    index: int = 0
    delta: ChatCompletionChunkDelta
    finish_reason: str | None = None


class ChatCompletionChunk(BaseModel):
    id: str
    object: Literal["chat.completion.chunk"] = "chat.completion.chunk"
    created: int
    model: str
    choices: list[ChatCompletionChunkChoice]
    usage: ChatCompletionUsage | None = None


class ModelInfo(BaseModel):
    id: str
    object: Literal["model"] = "model"


class ModelList(BaseModel):
    object: Literal["list"] = "list"
    data: list[ModelInfo]


class BackendUpsertRequest(BaseModel):
    name: str
    type: Literal["ollama", "hosted"]
    endpoint: str
    model: str
    priority: int = 100
    cost_per_1k_in: float = 0.0
    cost_per_1k_out: float = 0.0


class BackendOut(BaseModel):
    id: UUID
    name: str
    type: str
    endpoint: str
    model: str
    priority: int
    enabled: bool


class ApiKeyCreateRequest(BaseModel):
    name: str
    token_budget: int = Field(gt=0)
    window_seconds: int = 60


class ApiKeyCreateResponse(BaseModel):
    id: UUID
    raw_key: str  # returned exactly once; only the hash is stored


class ErrorDetail(BaseModel):
    message: str
    type: str
    param: Any | None = None
