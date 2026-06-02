# models.py
# Data models used across the application.
# IdempotencyRecord maps 1:1 to a row in the SQLite table.

from enum import Enum
from typing import Optional
from pydantic import BaseModel


class KeyStatus(str, Enum):
    # Request is currently being forwarded to upstream.
    # If Aegis sees PENDING on a second request with the same key,
    # it returns 409 — in-flight duplicate. See DECISIONS.md D-004, D-007.
    PENDING = "PENDING"

    # Upstream responded. Cached response is stored.
    # Subsequent requests with the same key + same body get this cached response.
    COMPLETE = "COMPLETE"


class IdempotencyRecord(BaseModel):
    key: str
    fingerprint: str           # SHA-256 of request body. See DECISIONS.md D-005.
    status: KeyStatus
    response_status: Optional[int] = None
    response_body: Optional[str] = None
    response_headers: Optional[str] = None  # JSON-serialised dict
    created_at: float          # Unix timestamp — used for TTL check
