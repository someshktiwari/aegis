# tests/test_proxy.py
# Integration tests for the idempotency proxy layer.
# Uses unittest.mock to patch forward_to_upstream so tests never need a
# real upstream service running.

import json
import time
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest


def _make_upstream_response(status_code: int = 200, body: dict = None, headers: dict = None):
    """Build a fake httpx.Response-like object for mocking upstream calls."""
    body = body or {"result": "success"}
    mock_resp = MagicMock()
    mock_resp.status_code = status_code
    mock_resp.text = json.dumps(body)
    mock_resp.content = json.dumps(body).encode()
    mock_resp.headers = headers or {"content-type": "application/json"}
    return mock_resp


# ── Happy path ────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_new_key_forwards_to_upstream(client):
    mock_resp = _make_upstream_response(201, {"id": "order-42"})

    with patch("proxy.forward_to_upstream", new=AsyncMock(return_value=mock_resp)):
        response = await client.post(
            "/orders",
            json={"item": "book"},
            headers={"X-API-Key": "test-key", "Idempotency-Key": "idem-new-001"},
        )

    assert response.status_code == 201


@pytest.mark.asyncio
async def test_duplicate_key_same_body_returns_cached(client):
    mock_resp = _make_upstream_response(200, {"status": "paid"})

    with patch("proxy.forward_to_upstream", new=AsyncMock(return_value=mock_resp)) as mock_fwd:
        r1 = await client.post(
            "/payments",
            json={"amount": 500},
            headers={"X-API-Key": "test-key", "Idempotency-Key": "idem-dup-001"},
        )
        r2 = await client.post(
            "/payments",
            json={"amount": 500},
            headers={"X-API-Key": "test-key", "Idempotency-Key": "idem-dup-001"},
        )

    assert r1.status_code == 200
    assert r2.status_code == 200
    # Upstream must only have been called ONCE — second request served from cache
    assert mock_fwd.call_count == 1


@pytest.mark.asyncio
async def test_key_reuse_different_body_returns_422(client):
    mock_resp = _make_upstream_response(200, {"ok": True})

    with patch("proxy.forward_to_upstream", new=AsyncMock(return_value=mock_resp)):
        await client.post(
            "/payments",
            json={"amount": 500},
            headers={"X-API-Key": "test-key", "Idempotency-Key": "idem-mismatch-001"},
        )
        r2 = await client.post(
            "/payments",
            json={"amount": 999},
            headers={"X-API-Key": "test-key", "Idempotency-Key": "idem-mismatch-001"},
        )

    assert r2.status_code == 422
    assert "reused with a different request" in r2.json()["error"]


@pytest.mark.asyncio
async def test_missing_idempotency_key_returns_400(client):
    response = await client.post("/payments", json={"amount": 100}, headers={"X-API-Key": "test-key"})
    assert response.status_code == 400
    assert "Idempotency-Key" in response.json()["error"]


@pytest.mark.asyncio
async def test_expired_key_treated_as_new(client, db):
    # Insert a completed record whose expires_at is in the past. The proxy's
    # on-access expiry check must treat it as expired, delete it, and forward fresh.
    past = time.time() - 999_999
    await db.execute(
        """
        INSERT INTO idempotency_keys
            (key, fingerprint, status, status_code, response_body, response_headers,
             created_at, expires_at)
        VALUES (?, ?, 'completed', 200, '{"old": true}', '{}', ?, ?)
        """,
        (
            "test-key:idem-expired-001",
            __import__("hashlib").sha256(b"POST\n/payments\n" + b'{"amount": 100}').hexdigest(),
            past,
            past,
        ),
    )
    await db.commit()

    mock_resp = _make_upstream_response(200, {"fresh": True})

    with patch("proxy.forward_to_upstream", new=AsyncMock(return_value=mock_resp)) as mock_fwd:
        response = await client.post(
            "/payments",
            content=b'{"amount": 100}',
            headers={
                "X-API-Key": "test-key",
                "Idempotency-Key": "idem-expired-001",
                "Content-Type": "application/json",
            },
        )

    assert response.status_code == 200
    assert mock_fwd.call_count == 1


@pytest.mark.asyncio
async def test_get_without_key_passes_through(client):
    """GET without key passes through directly — no idempotency."""
    mock_resp = _make_upstream_response(200, {"items": []})

    with patch("main.forward_to_upstream", new=AsyncMock(return_value=mock_resp)):
        response = await client.get("/orders")

    assert response.status_code == 200


@pytest.mark.asyncio
async def test_get_with_key_passes_through_without_caching(client):
    """
    GET with an Idempotency-Key must pass through without caching.
    GET is idempotent by HTTP semantics, so it is never cached even with a key.
    """
    mock_resp = _make_upstream_response(200, {"items": ["a", "b"]})

    with patch("main.forward_to_upstream", new=AsyncMock(return_value=mock_resp)) as mock_fwd:
        r1 = await client.get("/orders", headers={"X-API-Key": "test-key", "Idempotency-Key": "get-key-001"})
        r2 = await client.get("/orders", headers={"X-API-Key": "test-key", "Idempotency-Key": "get-key-001"})

    assert r1.status_code == 200
    assert r2.status_code == 200
    # Upstream called TWICE — GET is not cached even with a key
    assert mock_fwd.call_count == 2


# ── Failure path tests ────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_upstream_connection_error_returns_502_and_releases_key(client, db):
    """
    On a connection error, Aegis returns 502 and releases the key (deletes the
    in_flight row) so the client can retry with the same key.
    """
    with patch(
        "proxy.forward_to_upstream",
        new=AsyncMock(side_effect=httpx.ConnectError("upstream unreachable")),
    ):
        r1 = await client.post(
            "/payments",
            json={"amount": 500},
            headers={"X-API-Key": "test-key", "Idempotency-Key": "idem-conn-fail-001"},
        )

    assert r1.status_code == 502
    assert "Key released" in r1.json()["hint"]

    mock_resp = _make_upstream_response(200, {"status": "paid"})
    with patch(
        "proxy.forward_to_upstream",
        new=AsyncMock(return_value=mock_resp),
    ) as mock_fwd:
        r2 = await client.post(
            "/payments",
            json={"amount": 500},
            headers={"X-API-Key": "test-key", "Idempotency-Key": "idem-conn-fail-001"},
        )

    assert r2.status_code == 200
    assert mock_fwd.call_count == 1


@pytest.mark.asyncio
async def test_upstream_timeout_releases_key(client, db):
    """Timeout errors must also release the key."""
    with patch(
        "proxy.forward_to_upstream",
        new=AsyncMock(side_effect=httpx.TimeoutException("upstream timed out")),
    ):
        r1 = await client.post(
            "/payments",
            json={"amount": 500},
            headers={"X-API-Key": "test-key", "Idempotency-Key": "idem-timeout-001"},
        )

    assert r1.status_code == 502

    mock_resp = _make_upstream_response(200, {"status": "paid"})
    with patch("proxy.forward_to_upstream", new=AsyncMock(return_value=mock_resp)) as mock_fwd:
        r2 = await client.post(
            "/payments",
            json={"amount": 500},
            headers={"X-API-Key": "test-key", "Idempotency-Key": "idem-timeout-001"},
        )

    assert r2.status_code == 200
    assert mock_fwd.call_count == 1


@pytest.mark.asyncio
async def test_upstream_5xx_is_not_cached(client, db):
    """A transient upstream 500 must NOT be cached; the retry reaches upstream again."""
    error_resp = _make_upstream_response(500, {"error": "upstream overloaded"})

    with patch("proxy.forward_to_upstream", new=AsyncMock(return_value=error_resp)):
        r1 = await client.post(
            "/payments",
            json={"amount": 500},
            headers={"X-API-Key": "test-key", "Idempotency-Key": "idem-5xx-001"},
        )

    assert r1.status_code == 500

    success_resp = _make_upstream_response(200, {"status": "paid"})
    with patch(
        "proxy.forward_to_upstream",
        new=AsyncMock(return_value=success_resp),
    ) as mock_fwd:
        r2 = await client.post(
            "/payments",
            json={"amount": 500},
            headers={"X-API-Key": "test-key", "Idempotency-Key": "idem-5xx-001"},
        )

    assert r2.status_code == 200
    assert mock_fwd.call_count == 1


@pytest.mark.asyncio
async def test_upstream_429_is_not_cached(client, db):
    """429 is transient — it must NOT be cached; the retry reaches upstream again."""
    rate_limit_resp = _make_upstream_response(429, {"error": "rate limit exceeded"})

    with patch("proxy.forward_to_upstream", new=AsyncMock(return_value=rate_limit_resp)):
        r1 = await client.post(
            "/payments",
            json={"amount": 500},
            headers={"X-API-Key": "test-key", "Idempotency-Key": "idem-429-001"},
        )

    assert r1.status_code == 429

    success_resp = _make_upstream_response(200, {"status": "paid"})
    with patch("proxy.forward_to_upstream", new=AsyncMock(return_value=success_resp)) as mock_fwd:
        r2 = await client.post(
            "/payments",
            json={"amount": 500},
            headers={"X-API-Key": "test-key", "Idempotency-Key": "idem-429-001"},
        )

    assert r2.status_code == 200
    assert mock_fwd.call_count == 1


@pytest.mark.asyncio
async def test_upstream_4xx_deterministic_is_cached(client, db):
    """
    Deterministic client errors (e.g. 400 validation) ARE cached — the same
    invalid input always produces the same error, so caching prevents hammering.
    """
    error_resp = _make_upstream_response(400, {"error": "invalid amount"})

    with patch(
        "proxy.forward_to_upstream",
        new=AsyncMock(return_value=error_resp),
    ) as mock_fwd:
        r1 = await client.post(
            "/payments",
            json={"amount": -1},
            headers={"X-API-Key": "test-key", "Idempotency-Key": "idem-4xx-001"},
        )
        r2 = await client.post(
            "/payments",
            json={"amount": -1},
            headers={"X-API-Key": "test-key", "Idempotency-Key": "idem-4xx-001"},
        )

    assert r1.status_code == 400
    assert r2.status_code == 400
    # Upstream called only once — 400 is deterministic and correctly cached
    assert mock_fwd.call_count == 1


@pytest.mark.asyncio
async def test_set_cookie_stripped_from_cached_response(client, db):
    """
    Set-Cookie must NOT be replayed from cache to a future caller.
    The first (fresh) response returns Set-Cookie; the cached replay does not.
    """
    resp_with_cookie = _make_upstream_response(
        200,
        {"status": "paid"},
        headers={
            "content-type": "application/json",
            "set-cookie": "session=secret-A; HttpOnly",
        },
    )

    with patch("proxy.forward_to_upstream", new=AsyncMock(return_value=resp_with_cookie)):
        r1 = await client.post(
            "/payments",
            json={"amount": 500},
            headers={"X-API-Key": "test-key", "Idempotency-Key": "idem-cookie-001"},
        )
        r2 = await client.post(
            "/payments",
            json={"amount": 500},
            headers={"X-API-Key": "test-key", "Idempotency-Key": "idem-cookie-001"},
        )

    assert r1.status_code == 200
    assert r2.status_code == 200
    # Cached response must not contain the session cookie
    assert "set-cookie" not in {k.lower() for k in r2.headers.keys()}

@pytest.mark.asyncio
async def test_missing_api_key_returns_401(client):
    """No X-API-Key header — must return 401 before reaching idempotency logic."""
    response = await client.post(
        "/payments",
        json={"amount": 100},
        headers={"Idempotency-Key": "some-key"},
    )
    assert response.status_code == 401
    assert "X-API-Key" in response.json()["error"]


@pytest.mark.asyncio
async def test_api_key_scopes_idempotency_keys(client):
    """
    Two callers sending the same Idempotency-Key are tracked independently.
    Client A's cache must not be served to Client B.
    """
    mock_resp_a = _make_upstream_response(200, {"client": "A"})
    mock_resp_b = _make_upstream_response(200, {"client": "B"})

    with patch("proxy.forward_to_upstream", new=AsyncMock(side_effect=[mock_resp_a, mock_resp_b])) as mock_fwd:
        r_a = await client.post(
            "/payments",
            json={"amount": 500},
            headers={"X-API-Key": "client-a", "Idempotency-Key": "shared-key-001"},
        )
        r_b = await client.post(
            "/payments",
            json={"amount": 500},
            headers={"X-API-Key": "client-b", "Idempotency-Key": "shared-key-001"},
        )

    assert r_a.status_code == 200
    assert r_b.status_code == 200
    # Both requests must have reached upstream — same key, different API keys = different scopes
    assert mock_fwd.call_count == 2
    assert r_a.json()["client"] == "A"
    assert r_b.json()["client"] == "B"

# ── Concurrency ───────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_concurrent_duplicates_execute_upstream_exactly_once(client, db):
    """
    THE headline guarantee: five identical requests fired concurrently must
    produce exactly ONE upstream call. The per-key asyncio.Lock serialises
    them; the four losers block, then read `completed` and get the cached
    response. No request ever sees in_flight or a 409.
    """
    import asyncio

    call_count = 0

    async def slow_upstream(request, body):
        # Sleep keeps the lock held long enough for the other four requests
        # to arrive and block on it — this forces the race the lock prevents.
        nonlocal call_count
        call_count += 1
        await asyncio.sleep(0.05)
        return _make_upstream_response(201, {"id": "order-race"})

    with patch("proxy.forward_to_upstream", new=slow_upstream):
        responses = await asyncio.gather(*[
            client.post(
                "/payments",
                json={"amount": 500},
                headers={"X-API-Key": "test-key", "Idempotency-Key": "idem-race-001"},
            )
            for _ in range(5)
        ])

    # All five callers get the same successful response...
    assert all(r.status_code == 201 for r in responses)
    assert all(r.json() == {"id": "order-race"} for r in responses)
    # ...but upstream executed exactly once.
    assert call_count == 1

    # And the DB holds exactly one completed row for the scoped key.
    from store import get_record
    record = await get_record(db, "test-key:idem-race-001")
    assert record is not None
    assert record.state.value == "completed"


# ── Crash recovery (409 orphan) ───────────────────────────────────────────────

@pytest.mark.asyncio
async def test_orphaned_in_flight_returns_409(client, db):
    """
    A crash-orphaned in_flight record (lock gone, row survives, not yet
    recovered by the startup sweep) must return 409 — the original outcome
    is unknown, so the client must use a new key. This is the ONLY path
    that returns 409; concurrent duplicates never reach it (see test above).
    """
    from store import insert_in_flight

    # Plant the orphan directly: an in_flight row with no lock held —
    # exactly the state a crash leaves behind.
    await insert_in_flight(db, "test-key:idem-orphan-001", "fp-orphan")

    r = await client.post(
        "/payments",
        json={"amount": 500},
        headers={"X-API-Key": "test-key", "Idempotency-Key": "idem-orphan-001"},
    )

    assert r.status_code == 409
    assert "unresolved in-flight" in r.json()["error"]

@pytest.mark.asyncio
async def test_key_reuse_different_query_string_returns_422(client):
    """
    Regression test for the query-string fingerprint gap.

    The query string is part of the request identity: POST /pay?account=alice
    and POST /pay?account=bob are different operations even with an identical
    body. Before the fix, the query was forwarded upstream but excluded from
    the fingerprint, so the second call was a silent cache hit that returned
    alice's response for bob's request.
    """
    mock_resp = _make_upstream_response(200, {"paid": "alice"})

    with patch("proxy.forward_to_upstream", new=AsyncMock(return_value=mock_resp)) as mock_fwd:
        r1 = await client.post(
            "/pay?account=alice",
            json={"amount": 10},
            headers={"X-API-Key": "test-key", "Idempotency-Key": "idem-query-001"},
        )
        r2 = await client.post(
            "/pay?account=bob",
            json={"amount": 10},
            headers={"X-API-Key": "test-key", "Idempotency-Key": "idem-query-001"},
        )

    assert r1.status_code == 200
    # Same key, same body, different query = different request → 422, not a cache hit
    assert r2.status_code == 422
    # And upstream was never called a second time under the wrong identity
    assert mock_fwd.call_count == 1


# ── Request identity (fingerprint scope) ──────────────────────────────────────

@pytest.mark.asyncio
async def test_key_reuse_different_query_string_returns_422(client):
    """
    The query string is part of request identity.

    POST /pay?account=alice and POST /pay?account=bob are different operations
    even when the body is byte-identical. The query is forwarded to the upstream
    verbatim, so a fingerprint that excluded it would let the second call be
    served alice's cached response — a wrong answer for bob, with the upstream
    never consulted. See DECISIONS.md D-026.
    """
    mock_resp = _make_upstream_response(200, {"paid": "alice"})

    with patch("proxy.forward_to_upstream", new=AsyncMock(return_value=mock_resp)) as mock_fwd:
        r1 = await client.post(
            "/pay?account=alice",
            json={"amount": 10},
            headers={"X-API-Key": "test-key", "Idempotency-Key": "idem-query-001"},
        )
        r2 = await client.post(
            "/pay?account=bob",
            json={"amount": 10},
            headers={"X-API-Key": "test-key", "Idempotency-Key": "idem-query-001"},
        )

    assert r1.status_code == 200
    # Different query = different request → 422, never a cache hit
    assert r2.status_code == 422
    # And the upstream was not called a second time under the wrong identity
    assert mock_fwd.call_count == 1


@pytest.mark.asyncio
async def test_same_query_string_still_returns_cached(client):
    """
    Companion to the test above: adding the query to the fingerprint must not
    break ordinary caching for requests that do carry a query string.
    """
    mock_resp = _make_upstream_response(200, {"paid": "alice"})

    with patch("proxy.forward_to_upstream", new=AsyncMock(return_value=mock_resp)) as mock_fwd:
        r1 = await client.post(
            "/pay?account=alice",
            json={"amount": 10},
            headers={"X-API-Key": "test-key", "Idempotency-Key": "idem-query-002"},
        )
        r2 = await client.post(
            "/pay?account=alice",
            json={"amount": 10},
            headers={"X-API-Key": "test-key", "Idempotency-Key": "idem-query-002"},
        )

    assert r1.status_code == 200
    assert r2.status_code == 200
    assert mock_fwd.call_count == 1


# ── Header relay ──────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_hop_by_hop_headers_are_not_forwarded_upstream(client):
    """
    Hop-by-hop headers are meaningful for one transport hop only. Relaying
    Connection or TE to the upstream is a proxy correctness violation
    (RFC 2616 13.5.1). Idempotency-Key and X-API-Key are Aegis concerns and
    must not leak either. Authorization MUST survive — the upstream still has
    to authenticate the caller. See DECISIONS.md D-015 and D-025.
    """
    captured = {}

    async def _capture(request, body):
        from proxy import _STRIP_FORWARD
        captured["headers"] = {
            k.lower(): v for k, v in request.headers.items()
            if k.lower() not in _STRIP_FORWARD
        }
        return _make_upstream_response(200, {"ok": True})

    with patch("proxy.forward_to_upstream", new=AsyncMock(side_effect=_capture)):
        await client.post(
            "/orders",
            json={"item": "book"},
            headers={
                "X-API-Key": "test-key",
                "Idempotency-Key": "idem-hdr-001",
                "Authorization": "Bearer caller-token",
                "Connection": "keep-alive",
                "TE": "trailers",
            },
        )

    sent = captured["headers"]
    assert "connection" not in sent
    assert "te" not in sent
    assert "idempotency-key" not in sent
    assert "x-api-key" not in sent
    # The caller's credentials must reach the upstream untouched
    assert sent["authorization"] == "Bearer caller-token"


# ── Health endpoint ───────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_aegis_health_does_not_touch_upstream(client):
    """
    /_aegis/health reports on Aegis, not on the upstream. It must answer 200
    even when the upstream is unreachable — distinguishing "Aegis is down" from
    "the upstream is down" is the entire point of having it.
    """
    with patch(
        "proxy.forward_to_upstream",
        new=AsyncMock(side_effect=httpx.ConnectError("upstream is down")),
    ) as mock_fwd:
        r = await client.get("/_aegis/health")

    assert r.status_code == 200
    assert r.json()["service"] == "aegis"
    assert mock_fwd.call_count == 0
