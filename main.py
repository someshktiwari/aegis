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
from store import init_db, recover_stuck_in_flight


@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    FastAPI lifespan context manager.
    Code before `yield` runs on startup; code after runs on shutdown.

    Startup:
    - Open a single aiosqlite connection (shared across all requests via app.state)
    - Initialise the DB schema (idempotency_keys table + index)
    - Recover any in_flight records stuck from a previous crash
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
    recovered = await recover_stuck_in_flight(db)
    if recovered:
        print(f"[Aegis] Recovered {recovered} stuck in_flight record(s) from previous crash.")
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
        "A Stripe-style idempotency proxy service. "
        "Guarantees at-most-once execution for retried HTTP requests."
    ),
    version="1.1.0",
    lifespan=lifespan,
)


@app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
async def proxy_all(request: Request):
    """
    Catch-all route: every incoming request passes through this handler.

    Routing logic (in order):
    - Any non-GET without X-API-Key    → 401 (authentication required)
    - Non-GET with Idempotency-Key     → idempotency path (handle_request)
    - Non-GET without Idempotency-Key  → 400 (header required)
    - GET (with or without key)        → pass-through (GET is idempotent by HTTP semantics)

    Why X-API-Key is checked first:
    Authentication precedes all other validation. A request without a valid
    API key should never reach idempotency logic, regardless of what other
    headers it carries.

    Why GET is always pass-through:
    GET requests have no side effects — calling the same GET twice produces
    the same result without any duplicate work. Idempotency deduplication is
    only needed for state-mutating methods (POST, PUT, PATCH, DELETE).
    A GET with an Idempotency-Key header is passed through without caching —
    applying cache semantics to a GET would serve stale data for 24h.

    Header lookup uses FastAPI's Headers object for O(1) case-insensitive access
    rather than iterating all headers on every request.
    """
    # ── GET → pass-through first (no auth required for reads) ────────────────
    if request.method == "GET":
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

    # ── Non-GET: X-API-Key required ───────────────────────────────────────────
    # O(1) case-insensitive lookup via FastAPI's Headers object.
    if "x-api-key" not in request.headers:
        return JSONResponse(
            content={"error": "X-API-Key header is required"},
            status_code=401,
        )

    # ── Non-GET with Idempotency-Key → idempotency path ──────────────────────
    if "idempotency-key" in request.headers:
        return await handle_request(request, request.app.state.db)

    # ── Non-GET without Idempotency-Key → 400 ────────────────────────────────
    return JSONResponse(
        content={"error": "Idempotency-Key header is required for non-GET requests"},
        status_code=400,
    )