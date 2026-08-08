# proxy.py
# The heart of Aegis. Every proxied non-GET request flows through handle_request().
#
# Seven outcomes:
#   1. New key                → forward to upstream, cache response if cacheable
#   2. Cached (completed, same fingerprint) → return cached response immediately
#   3. Mismatched request     → 422 Unprocessable Entity (DECISIONS.md D-006)
#   4. Orphaned in_flight     → 409 Conflict — crash-recovery signal (DECISIONS.md D-007)
#   5. Failed record          → delete, treat as new key — retry allowed
#   6. Expired record         → delete, treat as new key
#   7. Upstream failure, three distinct paths:
#      a. Non-cacheable status (5xx/408/425/429) → record marked failed,
#         response passed through — retry allowed (DECISIONS.md D-021)
#      b. Connection error / timeout → record DELETED, 502 returned —
#         key released, safe to retry (no failed row is written)
#      c. Unexpected error after the upstream responded → record LEFT in_flight,
#         409 returned. The upstream may have executed; the outcome is unknown,
#         so the key is not released. See DECISIONS.md D-028.

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

# HTTP/1.1 hop-by-hop headers (RFC 2616 13.5.1). These are meaningful for a
# single transport hop only and must not be relayed by a proxy in either
# direction. content-length is included because httpx recalculates it.
_HOP_BY_HOP = frozenset([
    "connection", "keep-alive", "proxy-authenticate",
    "proxy-authorization", "te", "trailers",
    "transfer-encoding", "upgrade", "content-length",
])

# Response-only strip list: hop-by-hop plus content-encoding.
# content-encoding is not hop-by-hop per the RFC, but httpx auto-decompresses
# response bodies. Replaying `Content-Encoding: gzip` alongside bytes that have
# already been decompressed would make the client try to gunzip plain text and
# corrupt the response. See DECISIONS.md D-015.
_STRIP_RESPONSE = _HOP_BY_HOP | {"content-encoding"}

# Request-only strip list: hop-by-hop, plus the two headers that are Aegis
# concerns and must never reach the upstream, plus Host (httpx sets the correct
# Host for the target URL).
#
# content-encoding is deliberately NOT stripped here. Starlette hands us the
# request body exactly as it arrived — still compressed if the client
# compressed it — so removing the header while forwarding the compressed bytes
# would leave the upstream unable to decode its own payload. The asymmetry
# between this set and _STRIP_RESPONSE is the point: on the response side httpx
# has already decompressed, on the request side nothing has.
_STRIP_FORWARD = _HOP_BY_HOP | {"host", "idempotency-key", "x-api-key"}

# Response headers that must NEVER be stored in the DB or replayed from cache.
# Set-Cookie is the critical one: replaying client A's session cookie to
# client B is a direct session-leak vulnerability.
#
# This is a CACHE deny-list, not a forwarding deny-list. It applies only to
# response headers on their way into SQLite. A request's own Authorization
# header is forwarded to the upstream untouched — a transparent proxy that
# stripped caller credentials would break every authenticated upstream.
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
#
# Note this is an EXCLUSION rule, not an allow-list: every status not named
# here is cached, including 3xx redirects. See DECISIONS.md D-021.
_NON_CACHEABLE_STATUS = frozenset(range(500, 600)) | {408, 425, 429}


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
    4. Read + fingerprint the request (SHA-256 over method + path + query + body)
    5. Acquire per-key asyncio.Lock (guarded by registry lock — see lock_manager.py)
    6. Inside the lock:
       a. Look up scoped key in DB
       b. If found but expired   → delete, treat as new
       c. If found but failed    → delete, treat as new (retry allowed)
       d. If not found           → insert in_flight, forward, handle upstream result
       e. If in_flight           → 409 crash-recovery signal (orphaned from previous crash)
       f. If completed + wrong fingerprint → 422
       g. If completed + correct fingerprint → return cached response

    Steps 1 and 2 duplicate checks already performed in main.py's route. They
    are kept because handle_request is the module's public entry point and must
    hold its own contract rather than trusting its only current caller.
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
    fingerprint: str = compute_fingerprint(
        request.method, request.url.path, request.url.query, body
    )

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
                # Upstream unreachable, or the call timed out.
                # Delete in_flight so the key is not bricked — client may retry.
                await delete_record(db, scoped_key)
                return _json_response(
                    {
                        "error": "Upstream service unavailable.",
                        "hint": "Key released — safe to retry with the same Idempotency-Key.",
                    },
                    status_code=502,
                )
            except Exception:
                # Something other than a transport failure went wrong inside the
                # call. We cannot prove the upstream did not execute, so the row
                # stays in_flight and the key stays held. See D-028.
                return _unresolved_response()

            try:
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
            except Exception:
                # The upstream ran and we failed to record the outcome. Deleting
                # the row here would release the key and allow a retry to
                # execute the operation a second time — the one thing Aegis
                # exists to prevent. Leave it in_flight instead. See D-028.
                return _unresolved_response()

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
            return _unresolved_response()

        # Key reuse with a different request: 422. See DECISIONS.md D-006.
        # "Different" means any change to method, path, query or body — all four
        # are in the fingerprint (D-026).
        if record.fingerprint != fingerprint:
            return _json_response(
                {
                    "error": "Idempotency-Key reused with a different request.",
                    "hint": "Each Idempotency-Key must always be paired with the same method, path, query and body.",
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

    Strips hop-by-hop headers, Host, Idempotency-Key and X-API-Key. The last
    two are Aegis concerns only and the upstream should never see them.
    Everything else — including the caller's Authorization header — is relayed
    unchanged, because the upstream still has to authenticate the caller.

    The path and query string are both preserved: the query is part of what the
    upstream executes, which is also why it is part of the fingerprint (D-026).

    Note: upstream_resp.text stores the response as a decoded string.
    Aegis is scoped to JSON/text APIs — binary upstream responses are not
    supported. Documented as an explicit non-goal in DESIGN.md §11.
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
    """Strip hop-by-hop and content-encoding from a response before returning it."""
    return {
        k: v
        for k, v in headers.items()
        if k.lower() not in _STRIP_RESPONSE
    }


def _cacheable_headers(headers) -> dict:
    """Headers safe to store and replay. Strips hop-by-hop and sensitive headers."""
    return {
        k: v
        for k, v in headers.items()
        if k.lower() not in _STRIP_RESPONSE
        and k.lower() not in _DENY_CACHE_HEADERS
    }


def _unresolved_response() -> Response:
    """
    409 for a key whose outcome cannot be determined.

    Two situations produce it, and they are the same situation from the
    client's point of view: an in_flight row orphaned by a crash, and a request
    where Aegis failed after the upstream may already have executed. In both
    cases Aegis genuinely does not know whether the operation happened, and
    saying so is the only safe answer.
    """
    return _json_response(
        {
            "error": "This Idempotency-Key has an unresolved in-flight state.",
            "hint": "The original request outcome is unknown. Use a new Idempotency-Key.",
        },
        status_code=409,
    )


def _json_response(body: dict, status_code: int) -> Response:
    """Helper: return a JSON error response with correct Content-Type."""
    return Response(
        content=json.dumps(body),
        status_code=status_code,
        media_type="application/json",
    )
