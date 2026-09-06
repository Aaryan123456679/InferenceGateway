from __future__ import annotations

from aikit.db import session_scope

from gateway.backends.registry import BackendRegistry
from gateway.domain import BackendType
from gateway.persistence.repositories import BackendRepo
from tests.fakes import FakeModelClient


async def test_reload_populates_candidates_sorted_by_priority(sessionmaker):
    async with session_scope(sessionmaker) as session:
        repo = BackendRepo(session)
        await repo.upsert(
            name="slow", type=BackendType.OLLAMA, endpoint="e", model="m", priority=200
        )
        await repo.upsert(
            name="fast", type=BackendType.OLLAMA, endpoint="e", model="m", priority=100
        )

    registry = BackendRegistry(sessionmaker, lambda backend: FakeModelClient(name=backend.name))
    await registry.reload()

    candidates = registry.candidates("m")
    assert [b.name for b, _ in candidates] == ["fast", "slow"]


async def test_reload_excludes_disabled_and_unrelated_models(sessionmaker):
    async with session_scope(sessionmaker) as session:
        repo = BackendRepo(session)
        await repo.upsert(name="a", type=BackendType.OLLAMA, endpoint="e", model="model-a")
        await repo.upsert(name="b", type=BackendType.OLLAMA, endpoint="e", model="model-b")

    registry = BackendRegistry(sessionmaker, lambda backend: FakeModelClient(name=backend.name))
    await registry.reload()

    assert [b.name for b, _ in registry.candidates("model-a")] == ["a"]
    assert registry.candidates("model-c") == []


async def test_reload_closes_stale_clients(sessionmaker):
    closed: list[str] = []

    class TrackedFakeClient(FakeModelClient):
        async def aclose(self) -> None:
            closed.append(self.name)

    async with session_scope(sessionmaker) as session:
        await BackendRepo(session).upsert(
            name="only", type=BackendType.OLLAMA, endpoint="e", model="m"
        )

    registry = BackendRegistry(sessionmaker, lambda backend: TrackedFakeClient(name=backend.name))
    await registry.reload()
    await registry.reload()  # second reload should close the first round's client

    assert closed == ["only"]
