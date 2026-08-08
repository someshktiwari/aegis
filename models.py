# models.py
# Core data models for Aegis.
#
# IdempotencyRecord is a typed data-transfer object, not an ORM entity.
# The schema is owned by the hand-written DDL in store.py; this class is what
# store.get_record() hydrates a SQLite row into so the rest of the codebase
# works with attributes instead of tuple indices.
#
# Column-to-attribute mapping (the names differ deliberately — see D-027):
#   key      → idempotency_key
#   status   → state
# All other columns share their name with the attribute.

from enum import Enum
from typing import Optional

from pydantic import BaseModel


class State(str, Enum):
    """
    Three-state machine for an idempotency record lifecycle.
    in_flight  — request is being forwarded to upstream, not yet resolved
    completed  — upstream responded, cached response is stored
    failed     — upstream returned a non-cacheable status (5xx/408/425/429),
                 or the record was a crash orphan recovered on startup

    Note: connection errors and timeouts do NOT produce a failed row —
    the in_flight record is deleted and the client gets 502 (key released).
    See proxy.py's httpx.RequestError branch.

    Inheriting from `str` means a State compares equal to its own value, so
    it can be passed straight into a SQL parameter and read back out of a row
    with State(row[2]) without any conversion layer.
    """
    in_flight = "in_flight"
    completed = "completed"
    failed = "failed"


class IdempotencyRecord(BaseModel):
    """
    One row of the idempotency_keys table, as a typed Python object.
    One row per unique scoped key ("{api_key}:{idempotency_key}") the proxy
    has seen.

    Timestamps are Unix epoch floats (via time.time()) to match the SQLite
    REAL columns and the float comparisons in eviction.py. expires_at is
    computed once at insert time (created_at + ttl_seconds); the eviction
    sweep and the on-access check both compare against it directly.

    Why a plain Pydantic model and not an ORM entity:
    Aegis issues eight hand-written statements against a single table. An ORM
    would add a dependency, a metadata layer, and a second source of truth for
    the schema, in exchange for query-building Aegis does not need. The DDL in
    store.py is the schema; this class is the shape it comes back as.
    """
    idempotency_key: str
    fingerprint: str
    state: State = State.in_flight
    status_code: Optional[int] = None
    response_body: Optional[str] = None
    response_headers: Optional[str] = None
    created_at: float
    expires_at: float
