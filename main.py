# main.py
# FastAPI application entry point.
# Handles: app creation, lifespan (DB init + eviction task), catch-all proxy route.

import asyncio
from contextlib import asynccontextmanager

import aiosqlite
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response

from config import settings
from eviction import eviction_loop
from proxy import forward_to_upstream, handle_request
from store import init_db


@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    FastAPI lifespan context manager.
    Code before `yield` runs on startup; code after runs on shutdown.

    Startup:
    - Open a single aiosqlite connection (shared across all requests via app.state)
    - Initialise the DB schema (idempotency_keys table + index)
    - Start the background eviction task

    Shutdown:
    - Cancel the eviction task cleanly
    - Close the DB connection

    Why a single shared connection?
    SQLite supports multiple readers but only one writer at a time. Sharing one
    connection means all writes are serialised through one channel — this is
    safe and avoids "database is locked" errors that appear when multiple
    connections attempt concurrent writes.
    """
    db: aiosqlite.Connection = await aiosqlite.connect(settings.db_path)
    await init_db(db)
    app.state.db = db

    eviction_task = asyncio.create_task(eviction_loop(db))
    print(f"[Aegis] Started. Upstream: {settings.upstream_url} | DB: {settings.db_path}")

    yield  # ← App is running

    eviction_task.cancel()
    try:
        await eviction_task
    except asyncio.CancelledError:
        pass  # Expected — task was cancelled on shutdown

    await db.close()
    print("[Aegis] Shutdown complete.")


app = FastAPI(
    title="Aegis — Idempotency Proxy Service",
    description=(
        "A Stripe-style idempotency layer implemented as a FastAPI reverse proxy. "
        "Guarantees exactly-once semantics for non-idempotent HTTP operations."
    ),
    version="1.0.0",
    lifespan=lifespan,
)


@app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
async def proxy_all(request: Request):
    """
    Catch-all route: every incoming request passes through this handler.

    Routing logic:
    - Non-GET with Idempotency-Key  → idempotency path (handle_request)
    - Non-GET without Idempotency-Key → 400 (header required)
    - GET (with or without key)     → pass-through (GET is idempotent by HTTP semantics)

    Why GET is always pass-through:
    GET requests have no side effects — calling the same GET twice produces
    the same result without any duplicate work. Idempotency deduplication is
    only needed for state-mutating methods (POST, PUT, PATCH, DELETE).
    A GET with an Idempotency-Key header is passed through without caching —
    applying cache semantics to a GET would serve stale data for 24h, which
    is incorrect proxy behaviour.
    """
    has_key = "idempotency-key" in [h.lower() for h in request.headers.keys()]

    # ── Non-GET with key → idempotency path ──────────────────────────────────
    if has_key and request.method != "GET":
        return await handle_request(request, request.app.state.db)

    # ── Non-GET without key → 400 ─────────────────────────────────────────────
    if request.method != "GET":
        return JSONResponse(
            content={"error": "Idempotency-Key header is required for non-GET requests"},
            status_code=400,
        )

    # ── GET → pass-through (idempotent by HTTP semantics) ────────────────────
    # GET is always passed through regardless of whether a key is present.
    # Error handling: wrap in try/except so a down upstream returns 502,
    # not a raw 500 traceback.
    body = await request.body()
    try:
        upstream = await forward_to_upstream(request, body)
        return Response(
            content=upstream.content,
            status_code=upstream.status_code,
            media_type=upstream.headers.get("content-type"),
        )
    except Exception:
        return JSONResponse(
            content={"error": "Upstream service unavailable."},
            status_code=502,
        )
