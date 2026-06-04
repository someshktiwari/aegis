# proxy.py
# The heart of Aegis. Every proxied request flows through handle_request().
#
# Six outcomes:
#   1. New key                → forward to upstream, cache response if cacheable
#   2. Cached (completed, same fingerprint) → return cached response immediately
#   3. Mismatched body        → 422 Unprocessable Entity (DECISIONS.md D-006)
#   4. Orphaned in_flight     → 409 Conflict — crash-recovery signal (DECISIONS.md D-007)
#   5. Failed record          → delete, treat as new key — retry allowed
#   6. Upstream failure       → mark failed, pass through — key released, safe to retry

import json

import aiosqlite
import httpx
from fastapi import Request, Response

from config import settings
from eviction import is_expired
from fingerprint import compute_fingerprint
from lock_manager import lock_manager
from models import State
from store import (
    delete_record,
    get_record,
    insert_in_flight,
    update_complete,
    update_failed,
)

# HTTP/1.1 hop-by-hop headers (RFC 2616 13.5.1).
# Stripped from both forwarded requests and all responses (fresh + cached).
# content-encoding is included because httpx auto-decompresses the body,
# so replaying the content-encoding header alongside already-decompressed
# bytes would cause the client to corrupt the response trying to gunzip it.
_HOP_BY_HOP = frozenset([
    "connection", "keep-alive", "proxy-authenticate",
    "proxy-authorization", "te", "trailers",
    "transfer-encoding", "upgrade", "content-length",
    "content-encoding",
])

# Response headers that must NEVER be stored in the DB or replayed from cache.
# Set-Cookie is the critical one: replaying client A's session cookie to
# client B is a direct session-leak vulnerability.
# See DECISIONS.md D-022.
_DENY_CACHE_HEADERS = frozenset([
    "set-cookie",
    "www-authenticate",
    "authorization",
])

# HTTP status codes that must NOT be cached.
# 5xx: transient upstream failures — next retry should reach a healthy upstream.
# 408 Request Timeout: transient, not deterministic.
# 425 Too Early: transient (TLS early data).
# 429 Too Many Requests: transient — rate limit will reset.
# See DECISIONS.md D-021.
_NON_CACHEABLE_STATUS = frozenset(range(500, 600)) | {408, 425, 429}

# Headers stripped from the request before forwarding to upstream.
# Idempotency-Key and X-API-Key are Aegis concerns only — the upstream
# service should never see them.
_STRIP_FORWARD = frozenset(["host", "content-length", "idempotency-key", "x-api-key"])


async def handle_request(request: Request, db: aiosqlite.Connection) -> Response:
    """
    Main idempotency handler. Called by the catch-all route in main.py
    for non-GET requests that carry an Idempotency-Key header.

    Multi-tenancy: the X-API-Key header is required on every request.
    The DB lookup key is scoped per caller: "{api_key}:{idempotency_key}".
    Two callers sending the same Idempotency-Key value are tracked independently.

    Step-by-step:
    1. Read X-API-Key header (401 if missing)
    2. Read Idempotency-Key header (400 if missing)
    3. Form scoped DB key: "{api_key}:{idempotency_key}"
    4. Read + fingerprint request body (SHA-256 over method + path + body)
    5. Acquire per-key asyncio.Lock (guarded by registry lock — see lock_manager.py)
    6. Inside the lock:
       a. Look up scoped key in DB
       b. If found but expired   → delete, treat as new
       c. If found but failed    → delete, treat as new (retry allowed)
       d. If not found           → insert in_flight, forward, handle upstream result
       e. If in_flight           → 409 crash-recovery signal (orphaned from previous crash)
       f. If completed + wrong fingerprint → 422
       g. If completed + correct fingerprint → return cached response
    """
    # Multi-tenant API key — required on every request.
    api_key = request.headers.get("X-API-Key")
    if not api_key:
        return _json_response(
            {"error": "X-API-Key header is required"},
            status_code=401,
        )

    idempotency_key = request.headers.get("Idempotency-Key")
    if not idempotency_key:
        return _json_response(
            {"error": "Idempotency-Key header is required"},
            status_code=400,
        )

    # Scope the DB key per caller. Two clients sending the same Idempotency-Key
    # value are tracked independently — no cross-tenant cache hits or conflicts.
    scoped_key = f"{api_key}:{idempotency_key}"

    body: bytes = await request.body()
    fingerprint: str = compute_fingerprint(request.method, request.url.path, body)

    # Lock is held across the entire upstream call. See DECISIONS.md D-004.
    lock = await lock_manager.get(scoped_key)
    async with lock:
        record = await get_record(db, scoped_key)

        # Expired record: treat as brand-new key.
        if record is not None and is_expired(record.expires_at):
            await delete_record(db, scoped_key)
            record = None

        # Failed record: allow retry, treat as brand-new key.
        # A failed record (5xx/429 upstream or a recovered crash) is retryable.
        if record is not None and record.state == State.failed:
            await delete_record(db, scoped_key)
            record = None

        # New key: forward to upstream.
        if record is None:
            await insert_in_flight(db, scoped_key, fingerprint)

            try:
                upstream_resp = await forward_to_upstream(request, body)
            except httpx.RequestError:
                # Upstream unreachable or timed out.
                # Delete in_flight so the key is not bricked — client may retry.
                await delete_record(db, scoped_key)
                return _json_response(
                    {
                        "error": "Upstream service unavailable.",
                        "hint": "Key released — safe to retry with the same Idempotency-Key.",
                    },
                    status_code=502,
                )

            # Non-cacheable response: mark failed, pass through. Retry allowed.
            if upstream_resp.status_code in _NON_CACHEABLE_STATUS:
                await update_failed(
                    db,
                    scoped_key,
                    upstream_resp.status_code,
                    upstream_resp.text,
                    json.dumps(_cacheable_headers(upstream_resp.headers)),
                )
                return Response(
                    content=upstream_resp.content,
                    status_code=upstream_resp.status_code,
                    headers=_response_headers(upstream_resp.headers),
                )

            # Cache the response. Store sanitised headers.
            cached_hdrs = _cacheable_headers(upstream_resp.headers)
            await update_complete(
                db,
                scoped_key,
                upstream_resp.status_code,
                upstream_resp.text,
                json.dumps(cached_hdrs),
            )

            # Return full headers (incl. Set-Cookie) to the real first caller.
            return Response(
                content=upstream_resp.content,
                status_code=upstream_resp.status_code,
                headers=_response_headers(upstream_resp.headers),
            )

        # Orphaned in_flight: 409 (crash-recovery signal only).
        # Normal concurrent duplicates never reach this branch —
        # they block on the lock and receive the cached response.
        # See DECISIONS.md D-007.
        if record.state == State.in_flight:
            return _json_response(
                {
                    "error": "This Idempotency-Key has an unresolved in-flight state.",
                    "hint": "The original request outcome is unknown. Use a new Idempotency-Key.",
                },
                status_code=409,
            )

        # Key reuse with different body: 422. See DECISIONS.md D-006.
        if record.fingerprint != fingerprint:
            return _json_response(
                {
                    "error": "Idempotency-Key reused with a different request body.",
                    "hint": "Each Idempotency-Key must always be paired with the same request body.",
                },
                status_code=422,
            )

        # Cache hit: completed + matching fingerprint.
        cached_headers = (
            json.loads(record.response_headers) if record.response_headers else {}
        )
        return Response(
            content=record.response_body,
            status_code=record.status_code,
            headers=cached_headers,
        )


async def forward_to_upstream(request: Request, body: bytes) -> httpx.Response:
    """
    Forward the original request to the configured upstream URL.
    Strips hop-by-hop headers, Host, Content-Length, Idempotency-Key,
    and X-API-Key — these are Aegis concerns only, never forwarded upstream.

    Note: upstream_resp.text stores the response as a decoded string.
    Aegis is scoped to JSON APIs — binary upstream responses are not supported.
    See DECISIONS.md D-024 (binary response limitation).
    """
    target_url = f"{settings.upstream_url}{request.url.path}"
    if request.url.query:
        target_url += f"?{request.url.query}"

    forward_headers = {
        k: v
        for k, v in request.headers.items()
        if k.lower() not in _STRIP_FORWARD
    }

    async with httpx.AsyncClient(timeout=settings.upstream_timeout_seconds) as client:
        response = await client.request(
            method=request.method,
            url=target_url,
            content=body,
            headers=forward_headers,
        )

    return response


def _response_headers(headers) -> dict:
    """Strip hop-by-hop headers from a response before returning to the client."""
    return {
        k: v
        for k, v in headers.items()
        if k.lower() not in _HOP_BY_HOP
    }


def _cacheable_headers(headers) -> dict:
    """Headers safe to store and replay. Strips hop-by-hop and sensitive headers."""
    return {
        k: v
        for k, v in headers.items()
        if k.lower() not in _HOP_BY_HOP
        and k.lower() not in _DENY_CACHE_HEADERS
    }


def _json_response(body: dict, status_code: int) -> Response:
    """Helper: return a JSON error response with correct Content-Type."""
    return Response(
        content=json.dumps(body),
        status_code=status_code,
        media_type="application/json",
    )