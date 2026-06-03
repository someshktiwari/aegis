# models.py
# Core data models for Aegis.
# IdempotencyRecord maps 1:1 to a row in the SQLite idempotency store.

from enum import Enum
from typing import Optional
from sqlmodel import SQLModel, Field
from datetime import datetime, timedelta


class State(str, Enum):
    """
    Three-state machine for an idempotency record lifecycle.
    in_flight  — request is being forwarded to upstream, not yet resolved
    completed  — upstream responded, cached response is stored
    failed     — upstream returned 5xx, timed out, or process crashed mid-request
    """
    in_flight = "in_flight"
    completed = "completed"
    failed = "failed"


class IdempotencyRecord(SQLModel, table=True):
    """
    Represents a single idempotency record in the SQLite store.
    One row per unique Idempotency-Key received by the proxy.
    """
    idempotency_key: str = Field(primary_key=True)
    api_key: str = Field(default=None, nullable=False)
    fingerprint: str = Field(default=None, nullable=False)
    state: State = Field(default=State.in_flight)
    status_code: Optional[int] = Field(default=None)
    response_headers: Optional[str] = Field(default=None)
    response_body: Optional[str] = Field(default=None)
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)
    expires_at: datetime = Field(default_factory=lambda: datetime.utcnow() + timedelta(hours=24))