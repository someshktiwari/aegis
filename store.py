# store.py
# All SQLite interactions. Uses aiosqlite so DB calls are non-blocking.
# See DECISIONS.md D-003 (why SQLite) and D-010 (why async-first).
#
# CRITICAL: Never import the synchronous `sqlite3` module anywhere in Aegis.
# A blocking sqlite3 call inside an async function freezes the entire event
# loop — all in-flight requests stall until that call completes.

import time
from typing import Optional

import aiosqlite

from config import settings
from models import IdempotencyRecord, State

# DDL — run once on startup via init_db()
CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS idempotency_keys (
    key             TEXT PRIMARY KEY,
    fingerprint     TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'in_flight',
    response_status INTEGER,
    response_body   TEXT,
    response_headers TEXT,
    created_at      REAL NOT NULL
)
"""

# Index to speed up the TTL eviction sweep (DELETE WHERE created_at < cutoff)
CREATE_INDEX_SQL = """
CREATE INDEX IF NOT EXISTS idx_created_at ON idempotency_keys (created_at)
"""


async def init_db(db: aiosqlite.Connection) -> None:
    """Create table and index if they don't exist. Safe to call on every startup."""
    await db.execute(CREATE_TABLE_SQL)
    await db.execute(CREATE_INDEX_SQL)
    await db.commit()


async def get_record(db: aiosqlite.Connection, key: str) -> Optional[IdempotencyRecord]:
    """
    Fetch a record by idempotency key.
    Returns None if the key has never been seen.
    """
    async with db.execute(
        """
        SELECT key, fingerprint, status,
               response_status, response_body, response_headers, created_at
        FROM idempotency_keys
        WHERE key = ?
        """,
        (key,),
    ) as cursor:
        row = await cursor.fetchone()

    if row is None:
        return None

    return IdempotencyRecord(
        key=row[0],
        fingerprint=row[1],
        status=State(row[2]),
        response_status=row[3],
        response_body=row[4],
        response_headers=row[5],
        created_at=row[6],
    )


async def insert_in_flight(db: aiosqlite.Connection, key: str, fingerprint: str) -> None:
    """
    Insert a new key in in_flight status before forwarding to upstream.
    The lock in proxy.py is held across the entire upstream call, so
    no concurrent request ever reads this in_flight state directly —
    they block on the lock. in_flight exists for crash recovery: if Aegis
    dies after this write, the next request finds the orphaned record
    and returns 409. See DECISIONS.md D-004.
    """
    await db.execute(
        """
        INSERT INTO idempotency_keys (key, fingerprint, status, created_at)
        VALUES (?, ?, 'in_flight', ?)
        """,
        (key, fingerprint, time.time()),
    )
    await db.commit()


async def update_complete(
    db: aiosqlite.Connection,
    key: str,
    response_status: int,
    response_body: str,
    response_headers: str,
) -> None:
    """
    Transition an in_flight record to completed and store the upstream response.
    All subsequent duplicate requests will get this cached response.
    """
    await db.execute(
        """
        UPDATE idempotency_keys
        SET status           = 'completed',
            response_status  = ?,
            response_body    = ?,
            response_headers = ?
        WHERE key = ?
        """,
        (response_status, response_body, response_headers, key),
    )
    await db.commit()


async def update_failed(
    db: aiosqlite.Connection,
    key: str,
    response_status: int,
    response_body: str,
    response_headers: str,
) -> None:
    """
    Transition an in_flight record to failed and store the upstream response.
    Client may retry with the same key — failed records are not cached.
    """
    await db.execute(
        """
        UPDATE idempotency_keys
        SET status           = 'failed',
            response_status  = ?,
            response_body    = ?,
            response_headers = ?
        WHERE key = ?
        """,
        (response_status, response_body, response_headers, key),
    )
    await db.commit()


async def recover_stuck_in_flight(db: aiosqlite.Connection) -> int:
    """
    On startup, transition any in_flight record older than 60 seconds to failed.
    These are records where Aegis crashed mid-request and will never complete.
    Returns number of records recovered.
    """
    cutoff = time.time() - 60
    cursor = await db.execute(
        """
        UPDATE idempotency_keys
        SET status = 'failed'
        WHERE status = 'in_flight'
        AND created_at < ?
        """,
        (cutoff,),
    )
    await db.commit()
    return cursor.rowcount


async def delete_record(db: aiosqlite.Connection, key: str) -> None:
    """Delete a single record by key. Used when an expired record is found on access."""
    await db.execute("DELETE FROM idempotency_keys WHERE key = ?", (key,))
    await db.commit()


async def delete_expired(db: aiosqlite.Connection) -> int:
    """
    Bulk-delete all rows older than TTL. Called by the background eviction loop.
    Returns number of rows deleted.
    See DECISIONS.md D-008 for the combined lazy + eager eviction strategy.
    """
    cutoff = time.time() - settings.ttl_seconds
    cursor = await db.execute(
        "DELETE FROM idempotency_keys WHERE created_at < ?", (cutoff,)
    )
    await db.commit()
    return cursor.rowcount