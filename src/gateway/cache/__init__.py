"""Cache — two-tier: L1 exact-hash (Redis), L2 embedding-similarity
(pgvector), orchestrated by TwoTierCache (B.3, B.4)."""
from __future__ import annotations

from dataclasses import dataclass

from gateway.domain import CacheTier


@dataclass(frozen=True)
class CacheHit:
    content: str
    tokens_in: int
    tokens_out: int
    tier: CacheTier
