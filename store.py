# store.py
# All SQLite interactions. Uses aiosqlite so DB calls are non-blocking.
# See DECISIONS.md D-003 (why SQLite) and D-010 (why async-first).
#
# This module owns the schema. The DDL below is the single source of truth for
# what a row looks like; models.IdempotencyRecord is the typed shape it is read
# back into. Column names and attribute names differ in two places (key →
# idempotency_key, status → state) and the mapping lives in get_record().
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
# expires_at stores the absolute expiry timestamp (created_at + ttl). Storing it
# per-row lets the eviction sweep do a single indexed comparison and allows
# different keys to carry different TTLs in future. See DECISIONS.md D-023.
CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS idempotency_keys (
    key             TEXT PRIMARY KEY,
    fingerprint     TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'in_flight',
    status_code     INTEGER,
    response_body   TEXT,
    response_headers TEXT,
    created_at      REAL NOT NULL,
    expires_at      REAL NOT NULL
)
"""

# Index on expires_at — the eviction sweep filters on it every cycle.
# DELETE FROM idempotency_keys WHERE expires_at < now
CREATE_INDEX_SQL = """
CREATE INDEX IF NOT EXISTS idx_expires_at ON idempotency_keys (expires_at)
"""


async def init_db(db: aiosqlite.Connection) -> None:
    """Create table and index if they don't exist. Safe to call on every startup."""
    await db.execute(CREATE_TABLE_SQL)
    await db.execute(CREATE_INDEX_SQL)
    await db.commit()


async def get_record(db: aiosqlite.Connection, key: str) -> Optional[IdempotencyRecord]:
    """
    Fetch a record by scoped key ("{api_key}:{idempotency_key}").
    Returns None if the key has never been seen.

    This is the one place a raw row becomes an IdempotencyRecord, so it is the
    one place that needs to know column order. Every other module works with
    attributes.
    """
    async with db.execute(
        """
        SELECT key, fingerprint, status,
               status_code, response_body, response_headers,
               created_at, expires_at
        FROM idempotency_keys
        WHERE key = ?
        """,
        (key,),
    ) as cursor:
        row = await cursor.fetchone()

    if row is None:
        return None

    return IdempotencyRecord(
        idempotency_key=row[0],
        fingerprint=row[1],
        state=State(row[2]),
        status_code=row[3],
        response_body=row[4],
        response_headers=row[5],
        created_at=row[6],
        expires_at=row[7],
    )


async def insert_in_flight(db: aiosqlite.Connection, key: str, fingerprint: str) -> None:
    """
    Insert a new key in in_flight status before forwarding to upstream.

    The write happens before the upstream call, not after — see D-014. The lock
    in proxy.py is held across the entire upstream call, so during normal
    operation no concurrent request ever reads this in_flight state directly;
    they block on the lock. in_flight exists for crash recovery: if Aegis dies
    after this write, the row survives and the next request finds an orphan.

    expires_at is set at insert time as created_at + ttl_seconds.
    """
    now = time.time()
    expires_at = now + settings.ttl_seconds
    await db.execute(
        """
        INSERT INTO idempotency_keys (key, fingerprint, status, created_at, expires_at)
        VALUES (?, ?, 'in_flight', ?, ?)
        """,
        (key, fingerprint, now, expires_at),
    )
    await db.commit()


async def update_complete(
    db: aiosqlite.Connection,
    key: str,
    status_code: int,
    response_body: str,
    response_headers: str,
) -> None:
    """
    Transition an in_flight record to completed and store the upstream response.
    All subsequent duplicate requests will be served this cached response until
    the record expires.
    """
    await db.execute(
        """
        UPDATE idempotency_keys
        SET status           = 'completed',
            status_code      = ?,
            response_body    = ?,
            response_headers = ?
        WHERE key = ?
        """,
        (status_code, response_body, response_headers, key),
    )
    await db.commit()


async def update_failed(
    db: aiosqlite.Connection,
    key: str,
    status_code: int,
    response_body: str,
    response_headers: str,
) -> None:
    """
    Transition an in_flight record to failed and store the upstream response.

    The response is written but will never be replayed: proxy.py deletes a
    failed record on next access and re-runs the request fresh. It is retained
    purely so an operator inspecting the database after an incident can see
    what the upstream actually returned before the key was retried.
    """
    await db.execute(
        """
        UPDATE idempotency_keys
        SET status           = 'failed',
            status_code      = ?,
            response_body    = ?,
            response_headers = ?
        WHERE key = ?
        """,
        (status_code, response_body, response_headers, key),
    )
    await db.commit()


async def recover_stuck_in_flight(db: aiosqlite.Connection) -> int:
    """
    On startup, transition any in_flight record older than
    settings.in_flight_recovery_seconds to failed. These are records where
    Aegis died mid-request; nothing in the new process will ever resolve them.
    Returns the number of records recovered.

    The filter is on created_at, not expires_at: recovery is about how long ago
    the request started, not when the key expires. expires_at is 24 hours in
    the future and would never match.

    Records younger than the cutoff are left alone — they may belong to a
    legitimate slow upstream call in a process that has not actually crashed.
    Those still return 409 until they age out or the next restart sweeps them.
    """
    cutoff = time.time() - settings.in_flight_recovery_seconds
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
    """
    Delete a single record by key.
    Used when an expired or failed record is found on access, and to release
    the key when the upstream could not be reached at all.
    """
    await db.execute("DELETE FROM idempotency_keys WHERE key = ?", (key,))
    await db.commit()


async def delete_expired(db: aiosqlite.Connection) -> int:
    """
    Bulk-delete all rows whose expires_at is in the past. Called by the
    background eviction loop. Returns number of rows deleted.
    Uses the stored expires_at column (indexed) rather than recomputing
    created_at + ttl on every sweep. See DECISIONS.md D-008.
    """
    now = time.time()
    cursor = await db.execute(
        "DELETE FROM idempotency_keys WHERE expires_at < ?", (now,)
    )
    await db.commit()
    return cursor.rowcount
