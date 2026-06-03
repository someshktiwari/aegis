# tests/test_store.py
# Unit tests for store.py — the SQLite CRUD layer.
# These tests don't touch proxy logic or HTTP; they verify DB operations directly.

import time

import pytest

from models import State
from store import (
    delete_expired,
    delete_record,
    get_record,
    insert_in_flight,
    update_complete,
)


@pytest.mark.asyncio
async def test_insert_in_flight_creates_record(db):
    await insert_in_flight(db, "key-001", "fp-aaa")
    record = await get_record(db, "key-001")

    assert record is not None
    assert record.idempotency_key == "key-001"
    assert record.fingerprint == "fp-aaa"
    assert record.state == State.in_flight
    assert record.status_code is None
    assert record.response_body is None


@pytest.mark.asyncio
async def test_get_record_returns_none_for_unknown_key(db):
    record = await get_record(db, "nonexistent-key")
    assert record is None


@pytest.mark.asyncio
async def test_update_complete_transitions_status(db):
    await insert_in_flight(db, "key-002", "fp-bbb")
    await update_complete(db, "key-002", 200, '{"status": "ok"}', '{"content-type": "application/json"}')

    record = await get_record(db, "key-002")
    assert record.state == State.completed
    assert record.status_code == 200
    assert record.response_body == '{"status": "ok"}'


@pytest.mark.asyncio
async def test_delete_record_removes_row(db):
    await insert_in_flight(db, "key-003", "fp-ccc")
    await delete_record(db, "key-003")
    record = await get_record(db, "key-003")
    assert record is None


@pytest.mark.asyncio
async def test_delete_expired_removes_old_rows(db):
    # Insert a record with a timestamp far in the past
    await db.execute(
        "INSERT INTO idempotency_keys (key, fingerprint, status, created_at) VALUES (?, ?, 'completed', ?)",
        ("old-key", "fp-old", time.time() - 999_999),
    )
    await db.commit()

    deleted = await delete_expired(db)
    assert deleted == 1
    assert await get_record(db, "old-key") is None


@pytest.mark.asyncio
async def test_delete_expired_preserves_fresh_rows(db):
    await insert_in_flight(db, "fresh-key", "fp-fresh")
    deleted = await delete_expired(db)
    assert deleted == 0
    assert await get_record(db, "fresh-key") is not None
