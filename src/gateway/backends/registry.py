"""BackendRegistry — hot-reloadable pool of live `ModelClient` instances,
one per enabled `backend` row, grouped by the model name clients request."""
from __future__ import annotations

import uuid
from collections import defaultdict
from collections.abc import Callable

from aikit.db import session_scope
from aikit.model_client import ModelClient
from aikit.model_client.hosted import HostedClient
from aikit.model_client.ollama import OllamaClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from gateway.domain import Backend, BackendType
from gateway.persistence.repositories import BackendRepo


class ConfigError(Exception):
    """A backend row can't be turned into a working ModelClient."""


def default_client_factory(
    backend: Backend,
    *,
    ollama_host: str,
    hosted_api_key: str | None,
) -> ModelClient:
    """DIP: registry depends on this factory signature, not on
    OllamaClient/HostedClient directly — tests inject a fake instead."""
    if backend.type == BackendType.OLLAMA:
        return OllamaClient(
            backend.endpoint or ollama_host, default_model=backend.model, name=backend.name
        )
    if backend.type == BackendType.HOSTED:
        if not hosted_api_key:
            raise ConfigError(f"backend {backend.name!r} is hosted but no API key is configured")
        return HostedClient(
            backend.endpoint, hosted_api_key, default_model=backend.model, name=backend.name
        )
    raise ConfigError(f"unknown backend type: {backend.type}")


class BackendRegistry:
    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        client_factory: Callable[[Backend], ModelClient],
    ) -> None:
        self._sessionmaker = sessionmaker
        self._client_factory = client_factory
        self._clients: dict[uuid.UUID, ModelClient] = {}
        self._by_model: dict[str, list[Backend]] = {}

    async def reload(self) -> None:
        async with session_scope(self._sessionmaker) as session:
            backends = await BackendRepo(session).list_all()

        old_clients = list(self._clients.values())
        new_clients: dict[uuid.UUID, ModelClient] = {}
        by_model: dict[str, list[Backend]] = defaultdict(list)

        for backend in backends:
            if not backend.enabled:
                continue
            new_clients[backend.id] = self._client_factory(backend)
            by_model[backend.model].append(backend)

        for backends_for_model in by_model.values():
            backends_for_model.sort(key=lambda b: b.priority)

        self._clients = new_clients
        self._by_model = dict(by_model)

        for client in old_clients:
            await client.aclose()

    def candidates(self, model: str) -> list[tuple[Backend, ModelClient]]:
        """Enabled backends serving `model`, in priority order (ties broken
        by a routing policy over this same list)."""
        return [(b, self._clients[b.id]) for b in self._by_model.get(model, [])]

    async def aclose(self) -> None:
        for client in self._clients.values():
            await client.aclose()
        self._clients = {}
        self._by_model = {}
