"""Integration fixtures against real Postgres + Redis. Postgres-specific
features (pgvector cosine search) and Redis Lua scripting (breaker,
ratelimit) are core to this design, and fakeredis has no EVAL/EVALSHA
support — these need the real thing, not a fake (same lesson as
eval-platform's conftest, B.13 #2).

Defaults to the same DATABASE_URL/REDIS_URL the app and CI already use
(set PYTEST_DATABASE_URL/PYTEST_REDIS_URL instead to target separate
scratch instances). Schema is created fresh per test session by running
the Alembic migration and dropped after.
"""
from __future__ import annotations

import os

import pytest
from aikit.db import make_engine, make_sessionmaker
from alembic import command
from alembic.config import Config
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import async_sessionmaker

TEST_DATABASE_URL = os.environ.get("PYTEST_DATABASE_URL") or os.environ.get(
    "DATABASE_URL", "postgresql+asyncpg://gateway@127.0.0.1:5544/gateway_test"
)
TEST_REDIS_URL = os.environ.get("PYTEST_REDIS_URL") or os.environ.get(
    "REDIS_URL", "redis://127.0.0.1:6390/0"
)

_REPO_ROOT = os.path.dirname(os.path.dirname(__file__))


@pytest.fixture(scope="session", autouse=True)
def _apply_migrations():
    cfg = Config(os.path.join(_REPO_ROOT, "alembic.ini"))
    migrations_dir = os.path.join(_REPO_ROOT, "src/gateway/persistence/migrations")
    cfg.set_main_option("script_location", migrations_dir)
    cfg.set_main_option("sqlalchemy.url", TEST_DATABASE_URL)
    command.downgrade(cfg, "base")
    command.upgrade(cfg, "head")
    yield
    command.downgrade(cfg, "base")


@pytest.fixture
def sessionmaker() -> async_sessionmaker:
    engine = make_engine(TEST_DATABASE_URL)
    return make_sessionmaker(engine)


@pytest.fixture
async def redis():
    client = Redis.from_url(TEST_REDIS_URL)
    yield client
    await client.flushdb()
    await client.aclose()


@pytest.fixture(autouse=True)
async def _clean_tables(sessionmaker):
    yield
    async with sessionmaker() as session, session.begin():
        from sqlalchemy import text

        await session.execute(
            text(
                "TRUNCATE TABLE request_log, cache_entry, backend, api_key "
                "RESTART IDENTITY CASCADE"
            )
        )
