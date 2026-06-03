# models.py
# Core data models for Aegis.
# IdempotencyRecord maps 1:1 to a row in the SQLite idempotency store.

from enum import Enum
from typing import Optional

from sqlmodel import SQLModel, Field


class State(str, Enum):
    """
    Three-state machine for an idempotency record lifecycle.
    in_flight  — request is being forwarded to upstream, not yet resolved
    completed  — upstream responded, cached response is stored
    failed     — upstream returned 5xx/429, timed out, or process crashed mid-request
    """
    in_flight = "in_flight"
    completed = "completed"
    failed = "failed"


class IdempotencyRecord(SQLModel, table=True):
    """
    Represents a single idempotency record in the SQLite store.
    One row per unique Idempotency-Key received by the proxy.

    Timestamps are stored as Unix epoch floats (via time.time()) to match the
    SQLite REAL columns and the float-based comparisons in eviction.py.
    expires_at is computed once at insert time (created_at + ttl_seconds);
    the eviction sweep and on-access check both compare against it directly.
    """
    idempotency_key: str = Field(primary_key=True)
    fingerprint: str = Field(nullable=False)
    state: State = Field(default=State.in_flight)
    status_code: Optional[int] = Field(default=None)
    response_body: Optional[str] = Field(default=None)
    response_headers: Optional[str] = Field(default=None)
    created_at: float = Field(nullable=False)
    expires_at: float = Field(nullable=False)