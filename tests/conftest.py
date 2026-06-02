# tests/conftest.py
# Shared pytest fixtures.
# Uses an in-memory SQLite DB so tests never touch the filesystem.

import pytest
import pytest_asyncio
import aiosqlite
from httpx import ASGITransport, AsyncClient

from main import app
from store import init_db


@pytest_asyncio.fixture
async def db():
    """
    Fresh in-memory SQLite DB for each test.
    ":memory:" means the DB lives only for the duration of the connection.
    """
    conn = await aiosqlite.connect(":memory:")
    await init_db(conn)
    yield conn
    await conn.close()


@pytest_asyncio.fixture
async def client(db):
    """
    HTTPX AsyncClient wired to the FastAPI app with in-memory DB injected.
    Bypasses network entirely — all requests go directly to the ASGI app.
    """
    app.state.db = db
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as ac:
        yield ac
