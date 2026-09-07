"""A stub Ollama server: implements just enough of the wire contract
(`POST /api/chat`, `GET /api/tags`) for `OllamaClient` to talk to it, and
answers instantly with a fixed response.

Purpose (B.15's own gap): the real load test's concurrency ceiling (3
users) was set by this sandbox's single CPU-only Ollama instance, not by
anything in the gateway. Running the *same* load test against this stub
instead isolates the gateway's own pipeline - auth, rate-limit reserve/
reconcile, cache read/write, routing, breaker checks, request_log writes -
from real model latency, and answers the fair follow-up question: "was the
gateway ever actually tested under real concurrency?"

Run: uvicorn loadtest.stub_backend:app --port 11500
Register: POST /admin/backends {"endpoint": "http://localhost:11500", ...}
"""
from __future__ import annotations

from fastapi import FastAPI
from pydantic import BaseModel

app = FastAPI()


class ChatRequest(BaseModel):
    model: str
    messages: list[dict]
    stream: bool = False


@app.post("/api/chat")
async def chat(body: ChatRequest) -> dict:
    return {
        "model": body.model,
        "message": {"role": "assistant", "content": "stub response"},
        "done": True,
        "done_reason": "stop",
        "prompt_eval_count": 10,
        "eval_count": 3,
    }


@app.get("/api/tags")
async def tags() -> dict:
    return {"models": [{"name": "stub"}]}
