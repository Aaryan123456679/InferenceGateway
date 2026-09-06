from __future__ import annotations

from aikit.model_client import ChatMessage

from gateway.orchestrator import (
    estimate_complexity,
    estimate_tokens,
    prompt_hash,
    prompt_text,
)


def test_estimate_tokens_scales_with_length():
    short = [ChatMessage(role="user", content="hi")]
    long = [ChatMessage(role="user", content="hi " * 200)]
    assert estimate_tokens(short) < estimate_tokens(long)
    assert estimate_tokens(short) >= 1


def test_estimate_complexity_is_normalized_and_monotonic():
    short = [ChatMessage(role="user", content="x" * 10)]
    long = [ChatMessage(role="user", content="x" * 10000)]
    assert 0.0 <= estimate_complexity(short) <= 1.0
    assert 0.0 <= estimate_complexity(long) <= 1.0
    assert estimate_complexity(short) < estimate_complexity(long)
    assert estimate_complexity(long) == 1.0  # clamped


def test_prompt_hash_is_stable_and_namespaced_by_model():
    messages = [ChatMessage(role="user", content="hello")]
    assert prompt_hash("model-a", messages) == prompt_hash("model-a", messages)
    assert prompt_hash("model-a", messages) != prompt_hash("model-b", messages)


def test_prompt_hash_distinguishes_message_content():
    a = [ChatMessage(role="user", content="hello")]
    b = [ChatMessage(role="user", content="goodbye")]
    assert prompt_hash("m", a) != prompt_hash("m", b)


def test_prompt_text_joins_message_contents():
    messages = [ChatMessage(role="system", content="sys"), ChatMessage(role="user", content="usr")]
    assert prompt_text(messages) == "sys\nusr"
