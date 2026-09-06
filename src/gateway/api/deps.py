"""FastAPI dependencies. Singletons (engine, registry, orchestrator, ...)
live on `app.state`, set up once in `main.create_app`; these functions just
read them back per-request (DIP: routers depend on these, never on
concrete clients directly)."""
from __future__ import annotations

import hashlib
from collections.abc import AsyncIterator
from typing import cast

from aikit.db import session_scope
from fastapi import Depends, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from gateway.config import GatewaySettings
from gateway.domain import ApiKey
from gateway.orchestrator import RequestOrchestrator
from gateway.persistence.repositories import ApiKeyRepo, BackendRepo


def get_settings(request: Request) -> GatewaySettings:
    return cast(GatewaySettings, request.app.state.settings)


async def get_session(request: Request) -> AsyncIterator[AsyncSession]:
    async with session_scope(request.app.state.sessionmaker) as session:
        yield session


def get_backend_repo(session: AsyncSession = Depends(get_session)) -> BackendRepo:
    return BackendRepo(session)


def get_orchestrator(request: Request) -> RequestOrchestrator:
    return cast(RequestOrchestrator, request.app.state.orchestrator)


async def require_api_key(
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> ApiKey:
    raw_key = request.headers.get("X-API-Key")
    if not raw_key:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="missing X-API-Key")
    key_hash = hashlib.sha256(raw_key.encode()).hexdigest()
    api_key = await ApiKeyRepo(session).get_by_hash(key_hash)
    if api_key is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid API key")
    return api_key


async def require_admin_key(
    request: Request, settings: GatewaySettings = Depends(get_settings)
) -> None:
    if request.headers.get("X-Admin-Key") != settings.admin_key:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid admin key")
