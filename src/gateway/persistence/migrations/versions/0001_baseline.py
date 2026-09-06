"""baseline schema (HLD+LLD.md B.2)

Revision ID: 0001
Revises:
Create Date: 2026-09-07

"""
from __future__ import annotations

from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

_DDL = """
CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE api_key (
  id                 uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  key_hash           char(64) NOT NULL UNIQUE,
  name               text NOT NULL,
  token_budget       integer NOT NULL CHECK (token_budget > 0),
  window_seconds     integer NOT NULL DEFAULT 60 CHECK (window_seconds > 0),
  enabled            boolean NOT NULL DEFAULT true,
  created_at         timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE backend (
  id                 uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  name               text NOT NULL UNIQUE,
  type               text NOT NULL CHECK (type IN ('ollama','hosted')),
  endpoint           text NOT NULL,
  model              text NOT NULL,
  priority           integer NOT NULL DEFAULT 100,
  enabled            boolean NOT NULL DEFAULT true,
  cost_per_1k_in     numeric(10,6) NOT NULL DEFAULT 0,
  cost_per_1k_out    numeric(10,6) NOT NULL DEFAULT 0,
  created_at         timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE cache_entry (
  id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  model        text NOT NULL,
  prompt_hash  char(64) NOT NULL,
  embedding    vector(384) NOT NULL,
  response     text NOT NULL,
  tokens_in    integer NOT NULL,
  tokens_out   integer NOT NULL,
  created_at   timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ix_cache_vec ON cache_entry
  USING hnsw (embedding vector_cosine_ops);
CREATE INDEX ix_cache_model_time ON cache_entry (model, created_at DESC);

CREATE TABLE request_log (
  id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  api_key_id     uuid NOT NULL REFERENCES api_key(id),
  backend_id     uuid REFERENCES backend(id),
  cache_tier     text CHECK (cache_tier IN ('l1','l2')),
  model          text NOT NULL,
  tokens_in      integer NOT NULL DEFAULT 0,
  tokens_out     integer NOT NULL DEFAULT 0,
  cost           numeric(12,6) NOT NULL DEFAULT 0,
  latency_ms     integer NOT NULL,
  status         integer NOT NULL,
  finish_reason  text,
  ts             timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ix_log_ts ON request_log (ts DESC);
"""

_DROP = """
DROP TABLE IF EXISTS request_log;
DROP TABLE IF EXISTS cache_entry;
DROP TABLE IF EXISTS backend;
DROP TABLE IF EXISTS api_key;
"""


def upgrade() -> None:
    for statement in _DDL.strip().split(";\n"):
        statement = statement.strip()
        if statement:
            op.execute(statement)


def downgrade() -> None:
    for statement in _DROP.strip().split(";\n"):
        statement = statement.strip()
        if statement:
            op.execute(statement)
